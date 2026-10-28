"""Passive commissioning metadata; no runtime, capture, or inference imports.

Only allowlisted configuration *states* leave this module. Module lookup does
not establish that native libraries import, assets load, or hardware works.
"""
from __future__ import annotations

import importlib.util
import math
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import stat
import struct
import subprocess
import sys
import sysconfig

MODULES = ("httpx", "pydantic", "websockets", "openai", "faster_whisper",
           "torch", "ultralytics", "nanoowl", "rclpy")
EXECUTABLES = ("piper", "aplay", "ros2")
FLAGS = {
    "PET_VOICE": False, "PET_STT": False, "PET_EMOTE": True,
    "PET_DASHBOARD": True, "AGENT_SIMULATE": False,
    "PERCEPTION_SIMULATE": False, "PICRAWLER_SIMULATE": False,
    "PICRAWLER_ACTUATORS_ENABLED": False,
}


def flag(name, default, environ):
    raw = environ.get(name)
    specified = raw is not None
    if name == "PET_DASHBOARD" and raw is not None and raw not in {"0", "1"}:
        return {"specified": True, "requested": None, "state": "error",
                "failure_reason": "PET_DASHBOARD must be 0 or 1 (launcher flag)"}
    if raw is None:
        requested = default
    elif raw.strip() == "" and name == "PET_EMOTE":
        # PET_EMOTE retains the runtime's legacy default-on empty semantics.
        requested = True
    elif raw.strip().lower() in {"1", "true", "yes", "on"}:
        requested = True
    elif raw.strip().lower() in {"", "0", "false", "no", "off"}:
        requested = False
    else:
        return {"specified": True, "requested": None, "state": "error",
                "failure_reason": f"{name} must be a boolean (0/1, false/true, no/yes, off/on)"}
    return {"specified": specified, "requested": requested,
            "state": "requested" if requested else "disabled"}


def active_configuration(environ):
    """Validate active common settings without printing any supplied value."""
    result = {}
    for name, default, convert, valid, description in (
        ("ROBOT_PORT", "8000", int, lambda x: 1 <= x <= 65535,
         "an integer from 1 to 65535"),
        ("ROBOT_TIMEOUT_S", "15", float, lambda x: math.isfinite(x) and x > 0,
         "a finite positive number"),
    ):
        try:
            value = convert(environ.get(name, default))
            if not valid(value):
                raise ValueError
            item = {"state": "configured", "specified": name in environ, "value": value}
        except (TypeError, ValueError, OverflowError):
            item = {"state": "error", "specified": name in environ,
                    "failure_reason": f"{name} must be {description}"}
        result[name] = item
    sink = environ.get("PET_AUDIO_SINK", "pi").strip().lower()
    result["PET_AUDIO_SINK"] = {"state": "configured", "specified": "PET_AUDIO_SINK" in environ,
                                "value": sink} if sink in {"pi", "local"} else {
        "state": "error", "specified": True, "failure_reason": "PET_AUDIO_SINK must be pi or local"}
    try:
        shlex.split(environ.get("PET_VOICE_PLAYER", "aplay -q"))
        result["PET_VOICE_PLAYER"] = {"state": "configured", "specified": "PET_VOICE_PLAYER" in environ}
    except ValueError:
        result["PET_VOICE_PLAYER"] = {"state": "error", "specified": True,
                                      "failure_reason": "PET_VOICE_PLAYER must contain valid shell quoting"}
    return result


def dependency_metadata():
    result = {}
    for name in MODULES:
        try:
            state = "available" if importlib.util.find_spec(name) is not None else "missing"
        except Exception:
            state = "error"
        result[name] = {"state": state, "probe": "module_spec", "import_tested": False}
    for name in EXECUTABLES:
        try:
            state = "available" if shutil.which(name) else "missing"
        except Exception:
            state = "error"
        result[name] = {"state": state, "probe": "executable_lookup", "execution_tested": False}
    return result


def build_metadata():
    """One read-only Git query, fixed arguments, no hooks or error disclosure."""
    try:
        # Ignore user Git environment/config, optional locks and executable paths.
        result = subprocess.run(
            ["git", "--no-optional-locks", "-C", str(Path(__file__).resolve().parents[1]),
             "rev-parse", "--verify", "HEAD"],
            env={"PATH": os.defpath, "LC_ALL": "C", "GIT_CONFIG_NOSYSTEM": "1",
                 "GIT_CONFIG_GLOBAL": os.devnull, "GIT_OPTIONAL_LOCKS": "0"},
            capture_output=True, text=True, timeout=1, check=False,
        )
        sha = result.stdout.strip()
        if result.returncode != 0 or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", sha):
            return {"state": "unknown", "commit": None, "working_tree": "not inspected"}
        return {"state": "available", "commit": sha, "working_tree": "not inspected"}
    except subprocess.TimeoutExpired:
        return {"state": "error", "commit": None, "failure_reason": "build query timed out"}
    except (OSError, UnicodeError):
        return {"state": "unknown", "commit": None, "failure_reason": "build query unavailable"}


def platform_metadata():
    try:
        release = platform.freedesktop_os_release()
    except (OSError, ValueError):
        release = {}
    # Do not report hostnames, usernames, Python executable/prefix, or full uname.
    def token(value):
        return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.+~-]{1,100}", value) else "unknown"
    return {
        "os": token(platform.system()), "distribution": token(release.get("ID")),
        "distribution_version": token(release.get("VERSION_ID")),
        "kernel_release": token(platform.release()), "kernel_machine": token(platform.machine()),
        "userspace_bits": struct.calcsize("P") * 8,
        "userspace_abi": token(sysconfig.get_config_var("MULTIARCH")),
        "python": {"implementation": token(platform.python_implementation()),
                   "version": ".".join(map(str, sys.version_info[:3]))},
    }


def humble_compatibility(facts):
    """Static ABI inventory only; it neither imports ROS nor accepts a device."""
    fields = (facts.get("os"), facts.get("distribution"), facts.get("distribution_version"),
              facts.get("kernel_machine"), facts.get("userspace_bits"),
              facts.get("python", {}).get("version"), facts.get("userspace_abi"))
    if any(value in (None, "unknown") for value in fields):
        state = "unknown"
    else:
        state = "compatible" if (
            fields[:3] == ("Linux", "ubuntu", "22.04") and fields[3] in {"aarch64", "arm64"}
            and fields[4] == 64 and fields[5].split(".")[:2] == ["3", "10"]
            and fields[6] in {"aarch64-linux-gnu", "aarch64-linux-gnueabi", "arm64-linux-gnu"}
        ) else "incompatible"
    return {"state": state, "scope": "Jammy arm64/Python 3.10 binary ABI inventory only",
            "ros_import_tested": False, "runtime_tested": False,
            "provisioning_authorized": False}


def configured_asset(name, environ, *, enabled=True, companion_suffix=None):
    """Stat only explicitly configured model files; never read model contents."""
    specified = bool(environ.get(name))
    if not enabled:
        return {"specified": specified, "state": "disabled"}
    if not specified:
        return {"specified": False, "state": "unconfigured"}
    paths = [environ[name]]
    if companion_suffix:
        paths.append(environ[name] + companion_suffix)
    try:
        exists = all(stat.S_ISREG(os.stat(path).st_mode) for path in paths)
        state = "available" if exists else "missing"
    except FileNotFoundError:
        state = "missing"
    except (OSError, ValueError):
        state = "error"
    return {"specified": True, "state": state, "probe": "file_metadata_only", "load_tested": False}


def local_report(*, environ=None, ros_humble=False, profile="interaction"):
    environ = os.environ if environ is None else environ
    flags = {name: flag(name, default, environ) for name, default in FLAGS.items()}
    active = active_configuration(environ)
    dependencies = dependency_metadata()
    facts = platform_metadata()
    voice = flags["PET_VOICE"]
    stt = flags["PET_STT"]
    assets = {"PET_VOICE_MODEL": configured_asset("PET_VOICE_MODEL", environ,
              enabled=voice["requested"] is True, companion_suffix=".json")}

    def optional_capability(setting, dependency, asset=None):
        if setting["state"] in {"disabled", "error"}:
            return {"state": setting["state"], "runtime_tested": False}
        states = [dependencies[dependency]["state"]]
        if asset is not None:
            states.append(asset["state"])
        state = next((s for s in ("error", "unconfigured", "missing") if s in states), "unknown")
        return {"state": state, "runtime_tested": False}

    capabilities = {
        "speech_output": optional_capability(voice, "piper", assets["PET_VOICE_MODEL"]),
        "speech_input": optional_capability(stt, "faster_whisper"),
        "llm": {"state": "unknown" if environ.get("LLM_BASE_URL") else "unconfigured",
                "specified": bool(environ.get("LLM_BASE_URL")), "runtime_tested": False},
        "camera_and_perception": {"state": "unknown", "runtime_tested": False},
        "body_expressions": {"state": "disabled", "source": "launcher default; runtime CLI not supplied"},
        "control_owner": {"state": "disabled", "acquired": False},
        "physical_motion": {"state": "unknown", "hardware_verified": False},
        "speaker_audibility": {"state": "unknown", "hardware_verified": False},
    }
    if voice["requested"] is True and active["PET_AUDIO_SINK"]["state"] == "error":
        capabilities["speech_output"]["state"] = "error"
    elif (voice["requested"] is True and active["PET_AUDIO_SINK"].get("value") == "local"
          and "PET_VOICE_PLAYER" not in environ and dependencies["aplay"]["state"] in {"missing", "error"}):
        capabilities["speech_output"]["state"] = dependencies["aplay"]["state"]
    errors = [item["failure_reason"] for item in (*flags.values(), *active.values())
              if item["state"] == "error"]
    report = {
        "profile": {"name": profile, "scope": "launcher defaults and specified environment flags; no runtime started",
                    "flags": flags, "active_settings": active,
                    "legacy_navigation": "not evaluated; simulation-only", "capabilities": capabilities},
        "platform": facts, "build": build_metadata(), "dependencies": dependencies, "assets": assets,
        "hardware_verified": False,
        "commissioning_gates": {"jetson_storage_journal": {"state": "unknown",
            "required_before": "device installations or deployment; owner acceptance remains open"},
            "hardware_acceptance": {"state": "unknown", "accepted": False}},
        "configuration_errors": errors,
    }
    if ros_humble:
        report["ros_humble"] = humble_compatibility(facts)
    return report

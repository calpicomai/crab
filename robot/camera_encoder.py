"""Explicit encoder selection. Hardware is never inferred from a public alias."""
from importlib import import_module


def hardware_mjpeg(bitrate):
    """Select the vendor V4L2 implementation, or fail without software fallback."""
    if type(bitrate) is str and bitrate.isdecimal():
        bitrate = int(bitrate)
    if type(bitrate) is not int or not 1_000_000 <= bitrate <= 12_000_000:
        raise ValueError("hardware MJPEG bitrate must be 1000000..12000000 bits/s")
    package = import_module("picamera2.encoders")
    if getattr(package, "_hw_encoder_available", None) is not True:
        raise RuntimeError("Picamera2 does not advertise hardware encoding on this platform")
    module = import_module("picamera2.encoders.mjpeg_encoder")
    encoder_class = module.MJPEGEncoder
    if encoder_class.__module__ != module.__name__:
        raise RuntimeError("MJPEG encoder is not the direct hardware implementation")
    base_class = import_module("picamera2.encoders.v4l2_encoder").V4L2Encoder
    if not issubclass(encoder_class, base_class):
        raise RuntimeError("MJPEG encoder is not a V4L2 implementation")
    return encoder_class(bitrate=bitrate), bitrate

"""Real browser + actual local MuJoCo API. Synthetic virtual world only."""
import argparse
import json
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import urlopen
from playwright.sync_api import sync_playwright


@contextmanager
def owned_server(args):
    if not args.serve:
        yield args.url
        return
    with socket.socket() as probe:
        probe.bind(('127.0.0.1',0));port=probe.getsockname()[1]
    url=f'http://127.0.0.1:{port}'
    with tempfile.TemporaryFile(mode='w+') as log:
        child=subprocess.Popen([args.server_python,'-m','lab','serve','--port',str(port)],
            cwd=Path(__file__).resolve().parents[2],stdout=log,stderr=subprocess.STDOUT)
        try:
            deadline=time.monotonic()+10
            while True:
                if child.poll() is not None:
                    log.seek(0);raise AssertionError('Owned Lab server failed: '+log.read()[-4000:])
                try:
                    with urlopen(url+'/api/state',timeout=.3) as response:
                        if response.status==200:break
                except OSError:pass
                if time.monotonic()>deadline:raise AssertionError('Owned Lab server startup timed out')
                time.sleep(.05)
            yield url
        finally:
            child.terminate()
            try:child.wait(timeout=3)
            except subprocess.TimeoutExpired:child.kill();child.wait(timeout=3)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--url',default='http://127.0.0.1:8765')
    parser.add_argument('--output',default=str(Path(tempfile.gettempdir())/'crab-lab-browser'))
    parser.add_argument('--serve',action='store_true',help='Start and clean up an owned loopback fixture server')
    parser.add_argument('--server-python',default=sys.executable)
    args=parser.parse_args()
    parsed=urlsplit(args.url)
    if parsed.scheme!='http' or parsed.hostname!='127.0.0.1' or parsed.username or parsed.password:
        raise SystemExit('Browser acceptance is loopback only')
    output=Path(args.output);output.mkdir(parents=True,exist_ok=True)
    with owned_server(args) as url,sync_playwright() as playwright:
        browser=playwright.chromium.launch(headless=True,
            executable_path=shutil.which('chromium') or shutil.which('chromium-browser'))
        page=browser.new_page(viewport={'width':1512,'height':1100},device_scale_factor=1)
        errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
        page.goto(url)
        page.wait_for_function("state && state.provenance==='mujoco_simulation'")
        assert page.locator('#play').inner_text()=='Run simulation'
        assert page.locator('#step').is_enabled()
        page.locator('[data-mode="crawl"]').click()
        page.wait_for_function("state.mode==='crawl'")
        page.locator('#play').click()
        page.wait_for_function("state.sim_time_s>1.0 && state.runtime.running")
        before=page.evaluate('state.evaluation_truth.root_position_m')
        page.wait_for_function("state.sim_time_s>4.0")
        after=page.evaluate('state.evaluation_truth.root_position_m')
        assert before!=after,'Physics body did not move'
        page.locator('#play').click()
        page.wait_for_function('!state.runtime.running')
        paused=page.evaluate('state.sim_time_s')
        page.wait_for_timeout(250)
        assert page.evaluate('state.sim_time_s')==paused
        page.locator('#step').click()
        page.wait_for_function(f'state.sim_time_s>{paused}')
        page.screenshot(path=str(output/'desktop-crawl.png'),full_page=True)
        page.locator('#scenario').select_option('sonar_dropout')
        page.locator('#seed').fill('42')
        page.locator('#reset').click()
        page.wait_for_function("state.seed===42 && state.sim_time_s===0")
        page.locator('[data-mode="crawl"]').click()
        page.wait_for_function("state.mode==='crawl'")
        page.locator('#play').click()
        page.wait_for_function("state.sim_time_s>.6 && state.status.state==='inhibited'")
        page.locator('#play').click()
        page.wait_for_function('!state.runtime.running')
        page.set_viewport_size({'width':390,'height':844})
        page.screenshot(path=str(output/'narrow-dropout.png'),full_page=True)
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth'), 'Narrow layout overflows'
        page.route('**/api/state',lambda route:route.abort())
        page.wait_for_function("document.getElementById('connection').textContent==='DISCONNECTED'",timeout=5000)
        assert page.locator('#play').is_disabled()
        page.screenshot(path=str(output/'narrow-disconnected.png'),full_page=True)
        page.unroute('**/api/state')
        page.wait_for_function("document.getElementById('connection').textContent==='LOCAL PHYSICS'")
        assert page.locator('#play').is_enabled()
        assert page.locator('#error').is_hidden()
        fault=page.evaluate('JSON.parse(JSON.stringify(state))')
        fault['runtime'].update(error='Physics stopped: injected fixture; showing last valid snapshot',running=False,snapshot_valid=False)
        page.route('**/api/state',lambda route:route.fulfill(json=fault))
        page.wait_for_function("document.getElementById('connection').textContent==='PHYSICS ERROR'")
        assert page.locator('#play').is_disabled() and page.locator('#step').is_disabled()
        assert page.locator('#reset').is_enabled()
        page.screenshot(path=str(output/'narrow-physics-error.png'),full_page=True)
        page.unroute('**/api/state')
        page.wait_for_function("document.getElementById('connection').textContent==='LOCAL PHYSICS'")
        assert not errors,errors
        print(json.dumps({'browser':'Chromium','actual_local_physics':True,'desktop_controls_and_motion':True,
            'pause_step_reset':True,'dropout_inhibition':True,'narrow_no_overflow':True,
            'disconnect_reconnect':True,'synthetic_error_view_reset_only':True,
            'javascript_errors':errors,'screenshots':str(output)},indent=2))
        browser.close()


if __name__=='__main__':main()

"""Real loopback HTTP/SSE startup with disabled real hardware and explicit fake camera."""
import base64
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

import httpx
from browser_live_feed import CDP, ROOT, port, wait_url

OUT = Path(sys.argv[1] if len(sys.argv)>1 else '/tmp/crab-dashboard-startup')
OUT.mkdir(exist_ok=True)
SERVER = '''
import os
import uvicorn
import robot.server as server
from robot.camera import PiCamera
from shared.vitals import SystemVitals, VitalsSampler
def unavailable(path):
    import time
    time.sleep(.12)
    raise OSError('synthetic unreadable host metric fixture')
class FixtureMetrics:
    def snapshot(self):
        import time
        return {'host':'pi','captured_at':time.time(),'age_s':0,'cpu_percent':23.5,
                'memory_used_mib':512,'memory_total_mib':4096,'temperature_c':42,
                'rss_mib':60,'process_cpu_percent':2,'source':'synthetic fixture, no hardware acceptance','reasons':{}}
collector = FixtureMetrics() if os.environ.get('CRAB_FIXTURE_METRICS_KNOWN')=='1' else SystemVitals('pi', reader=unavailable, thermal_root='/tmp/crab-no-thermal-fixture')
server._host_vitals = VitalsSampler('pi', collector=collector)
server.camera = PiCamera(width=960, height=720, simulate=True)
uvicorn.run(server.app, host='127.0.0.1', port=int(os.environ['PICRAWLER_PORT']))
'''
server_port, browser_port = port(), port()
base = f'http://127.0.0.1:{server_port}'
env = {**os.environ, 'PICRAWLER_SIMULATE':'0', 'PICRAWLER_SIM_WORLD':'0',
       'PICRAWLER_ACTUATORS_ENABLED':'0', 'PICRAWLER_HOME_ON_START':'none',
       'PICRAWLER_AUDIO_ENABLED':'0', 'PICRAWLER_CAMERA_ENABLED':'0',
       'PICRAWLER_ULTRASONIC_ENABLED':'0', 'PICRAWLER_BATTERY_ENABLED':'0',
       'PICRAWLER_HOST':'127.0.0.1', 'PICRAWLER_PORT':str(server_port)}
server = browser = cdp = None
with tempfile.TemporaryDirectory(prefix='crab-startup-browser-') as profile, \
     open(OUT/'server.log','w') as server_log, open(OUT/'chromium.log','w') as browser_log, \
     httpx.Client(timeout=2, trust_env=False) as http:
    try:
        server = subprocess.Popen([sys.executable,'-c',SERVER], cwd=ROOT, env=env, stdout=server_log, stderr=subprocess.STDOUT)
        wait_url(http, base+'/health', server)
        browser = subprocess.Popen([shutil.which('chromium'), '--headless','--disable-gpu','--no-sandbox',
            '--disable-dev-shm-usage','--disable-background-networking','--no-first-run',
            f'--remote-debugging-port={browser_port}',f'--user-data-dir={profile}','about:blank'],stdout=browser_log,stderr=subprocess.STDOUT)
        deadline=time.monotonic()+8
        while True:
            targets=wait_url(http,f'http://127.0.0.1:{browser_port}/json/list',browser).json()
            pages=[t for t in targets if t.get('type')=='page' and t.get('webSocketDebuggerUrl')]
            if pages:break
            if time.monotonic()>deadline:raise TimeoutError('Chromium did not create a page target')
            time.sleep(.03)
        cdp = CDP(pages[0]['webSocketDebuggerUrl'])
        for method in ('Runtime.enable','Network.enable','Page.enable'):
            cdp.call(method)
        cdp.call('Emulation.setDeviceMetricsOverride',{'width':1440,'height':1000,'deviceScaleFactor':1,'mobile':False})
        # Instrument dispatch only: never replace/mimic EventSource or fetch.
        cdp.call('Page.addScriptToEvaluateOnNewDocument',{'source': '''
window.startupProbe={opens:0,states:0};
const nativeAdd=EventSource.prototype.addEventListener;
EventSource.prototype.addEventListener=function(name,callback,...args){
  if(name==='state')return nativeAdd.call(this,name,function(event){startupProbe.states++;callback.call(this,event);},...args);
  return nativeAdd.call(this,name,callback,...args);
};
window.realNow=Date.now.bind(Date);Date.now=()=>realNow()+Number(sessionStorage.testSkew||0);
'''})
        cdp.call('Page.navigate',{'url':base+'/sim'})
        cdp.wait("typeof latestState!=='undefined' && latestState!==null && cameraNaturalWidth===960")
        now=time.time()
        payload={'_publisher_id':'unknown-input-fixture','_publisher_started_at_ns':time.time_ns(),'_sequence':1,
                 '_produced_at':now,'_produced_at_pi_lower':now-.01,'name':'Unknown inputs fixture',
                 'simulate':False,'stopped':True,'battery_v':None,'distance_cm':None,'audio':None,'hearing':None,'mind':None,
                 'inputs':{name:{'available':False,'provenance':'real','captured_at':None,'received_at':None,
                                'age_s':None,'max_age_s':2,'fresh':False,'used':False,'failure_reason':'missing fixture input'}
                           for name in ('battery','ultrasonic','camera','microphone','speaker','interpretation')},
                 'host_vitals':{'host':'jetson','age_s':0,'cpu_percent':None,'memory_used_mib':None,'temperature_c':None}}
        response=http.post(base+'/sim/brain',json=payload)
        response.raise_for_status();assert response.json()['ok']
        cdp.wait("document.querySelector('#petname').textContent==='Unknown inputs fixture'")
        good=cdp.value("({states:startupProbe.states,latest:latestState!==null,clock:clockAnchor!==null,source:document.querySelector('#provenance').textContent,motion:document.querySelector('#motion-status').textContent,host:latestState.host_vitals,battery:document.querySelector('#battery-value').textContent})")
        assert good['latest'] and good['clock'] and good['states']>0
        assert good['source']=='REAL INPUTS · UNVERIFIED'
        assert good['motion']=='ACTUATORS DISABLED'
        assert good['host']['temperature_c'] is None and good['host']['memory_used_mib'] is None
        cdp.value("sessionStorage.testSkew='300';location.reload();true")
        cdp.wait("typeof startupProbe!=='undefined' && startupProbe.states>=5 && typeof cameraNaturalWidth!=='undefined' && cameraNaturalWidth===960")
        bad=cdp.value("({states:startupProbe.states,latest:latestState!==null,clock:clockAnchor!==null,source:document.querySelector('#provenance').textContent,episode:document.querySelector('#episode').textContent,dimensions:document.querySelector('#camera-dimensions').textContent,rate:document.querySelector('#preview-rate').textContent})")
        assert bad['states']>0 and not bad['latest'] and not bad['clock']
        assert bad['source']=='REPORTED REAL · AGE UNKNOWN' and bad['episode']=='Pi timing unverified'
        cdp.wait("document.querySelector('#runtime-alert').textContent.includes('offset') && !document.querySelector('#runtime-alert').hidden")
        assert cdp.value("observedState!==null && document.querySelector('#resume-review').disabled")
        cdp.wait("cameraHz>0 && cameraReceivedHz>0 && !document.querySelector('#preview-rate').textContent.includes('—')")
        bad['explanation']=cdp.value("document.querySelector('#runtime-alert').textContent")
        bad['rate']=cdp.value("document.querySelector('#preview-rate').textContent")
        bad['dimensions']=cdp.value("document.querySelector('#camera-dimensions').textContent")
        assert cdp.value("graphHistory.every(p=>p.battery===null && p.pi_cpu_percent===null)")
        def screenshot(name):
            cdp.value("if(!document.getElementById('fixture-label'))document.querySelector('main').insertAdjacentHTML('afterbegin','<p id=\"fixture-label\" style=\"color:#edbf79;margin:0 0 16px\">SYNTHETIC STARTUP FIXTURE · NO PHYSICAL ACCEPTANCE</p>');true")
            size=cdp.call('Page.getLayoutMetrics')['cssContentSize']
            output=cdp.call('Page.captureScreenshot',{'format':'png','captureBeyondViewport':True,'clip':{'x':0,'y':0,'width':size['width'],'height':size['height'],'scale':1}})
            (OUT/name).write_bytes(base64.b64decode(output['data']))
        screenshot('clock-unverified-desktop.png')
        cdp.call('Emulation.setDeviceMetricsOverride',{'width':390,'height':844,'deviceScaleFactor':1,'mobile':True})
        cdp.wait("document.documentElement.scrollWidth<=window.innerWidth")
        screenshot('clock-unverified-mobile.png')
        cdp.call('Emulation.setDeviceMetricsOverride',{'width':1440,'height':1000,'deviceScaleFactor':1,'mobile':False})
        # Represent a long-running old preview, then reboot the actual fake server
        # while the browser clock is still untrusted. No page reload or fake SSE.
        old_boot=cdp.value("observedState.server_boot_id")
        cdp.value("cameraFetchedSequence='100000';true")
        # Abrupt reboot of this owned fake process; no device or shared service.
        server.kill();server.wait(timeout=3)
        server=subprocess.Popen([sys.executable,'-c',SERVER],cwd=ROOT,env={**env,'CRAB_FIXTURE_METRICS_KNOWN':'1'},stdout=server_log,stderr=subprocess.STDOUT)
        wait_url(http,base+'/health',server)
        cdp.wait("observedState!==null && observedState.server_boot_id!=="+json.dumps(old_boot)+" && cameraSequence!==null && Number(cameraSequence)<100000",timeout=10)
        reboot=cdp.value("({clock:clockAnchor!==null,latest:latestState!==null,preview_sequence:cameraSequence,resume_disabled:document.querySelector('#resume-review').disabled})")
        assert not reboot['clock'] and not reboot['latest'] and reboot['resume_disabled']
        # Recover without weakening the clock policy or recreating EventSource.
        cdp.value("sessionStorage.testSkew='0';location.reload();true")
        cdp.wait("typeof latestState!=='undefined' && latestState!==null && clockAnchor!==null")
        cdp.call('Network.emulateNetworkConditions',{'offline':False,'latency':300,'downloadThroughput':-1,'uploadThroughput':-1})
        cdp.wait("clockAnchor===null && clockReason.includes('round trip')",timeout=8)
        delayed=cdp.value("({reason:clockReason,probe:clockProbeResult,packets:stateObservedMessages,resume_disabled:document.querySelector('#resume-review').disabled})")
        assert delayed['resume_disabled'] and delayed['probe']['rtt_ms']>200
        cdp.call('Network.emulateNetworkConditions',{'offline':False,'latency':0,'downloadThroughput':-1,'uploadThroughput':-1})
        cdp.wait("clockAnchor!==null",timeout=8)
        cdp.wait("document.querySelector('#cpu-value').textContent.includes('23.5%') && graphHistory.some(p=>p.pi_cpu_percent===23.5)")
        partial=cdp.value("({phase:document.querySelector('#episode').textContent,cpu:document.querySelector('#cpu-value').textContent,memory:document.querySelector('#memory-value').textContent,temperature:document.querySelector('#temperature-value').textContent})")
        assert partial['phase']=='Companion not publishing'
        screenshot('pi-vitals-without-companion.png')
        responsive={}
        for width in (320,768,1024):
            cdp.call('Emulation.setDeviceMetricsOverride',{'width':width,'height':900,'deviceScaleFactor':1,'mobile':width<500})
            cdp.wait("document.documentElement.scrollWidth<=window.innerWidth")
            assert cdp.value("(()=>{const r=document.querySelector('#stop-control').getBoundingClientRect();return r.top>=0&&r.bottom<=window.innerHeight;})()")
            responsive[str(width)]={'no_overflow':True,'stop_visible':True}
        cdp.call('Emulation.setDeviceMetricsOverride',{'width':1440,'height':1000,'deviceScaleFactor':1,'mobile':False})

        # Exercise a real network stream disconnect/reconnect; never fake an event.
        cdp.call('Network.setBlockedURLs',{'urls':['*/sim/events*']})
        cdp.value("stateStream.close();stateStream=null;streamConnected=false;connectState();true")
        cdp.wait("document.querySelector('#feed-state').textContent.includes('disconnected')")
        cdp.wait("document.querySelector('#resume-review').disabled")
        cdp.call('Network.setBlockedURLs',{'urls':[]})
        cdp.wait("streamConnected && stateObservedMessages>2",timeout=8)
        reconnect=cdp.value("({states:stateObservedMessages,clock:clockAnchor!==null,latest:latestState!==null})")
        assert reconnect['latest'] and reconnect['clock']
        # A malformed real stream sample is rejected visibly, without killing
        # rendering or losing the independent camera/STOP paths.
        cdp.value("stateStream.dispatchEvent(new MessageEvent('state',{data:'broken-json'}));true")
        cdp.wait("stateRejected>0")
        assert cdp.value("document.querySelector('#stop-control').disabled===false")
        assert not cdp.errors,cdp.errors
        report={'scope':'Native Chromium EventSource over actual loopback HTTP/SSE; all hardware disabled, camera explicitly synthetic. Injected skew is not a device measurement.',
                'null_inputs_accepted':good,'injected_300ms_browser_skew':bad,'actual_sse_reconnect':reconnect,'untrusted_reboot_preview_recovers':reboot,'pi_vitals_without_companion':partial,'network_delay_rejected_visibly':delayed,'host_read_delay_s':.12,'responsive':responsive,'javascript_errors':cdp.errors}
        (OUT/'report.json').write_text(json.dumps(report,indent=2)+'\n')
        print(json.dumps(report,indent=2))
    finally:
        if cdp:cdp.close()
        for process in (browser,server):
            if process and process.poll() is None:
                process.terminate()
                try:process.wait(timeout=3)
                except subprocess.TimeoutExpired:process.kill();process.wait(timeout=3)

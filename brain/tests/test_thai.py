import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import numpy as np
import pytest
from jarvis.config import Config
from jarvis.core import normalise_command, Assistant
from jarvis import thai
from jarvis.prompts import build_system_prompt, classify_confirmation
from jarvis.stt import Transcriber
from jarvis.tools import execute, load_all, REGISTRY

@pytest.mark.parametrize('text,tool,args', [
 ('จาร์วิส เปิด Google Chrome ให้หน่อย','open_app',{'name':'Google Chrome'}),
 ('เปิด VS Code ให้หน่อย','open_app',{'name':'Visual Studio Code'}),
 ('ลดเสียงลงเหลือ 30 เปอร์เซ็นต์','system_control',{'action':'volume_set','value':30}),
 ('ลดเสียงเหลือ ๓๐ เปอร์เซ็นต์','system_control',{'action':'volume_set','value':30}),
 ('แล้วเปิด YouTube Music ด้วย','youtube_music',{'action':'open'}),
 ('เล่นเพลง Bruno Mars','youtube_music',{'action':'search','query':'bruno mars'}),
 ('หยุดเพลง','youtube_music',{'action':'pause'}),
 ('เล่นเพลงต่อ','youtube_music',{'action':'play'}),
 ('ข้ามเพลงนี้','youtube_music',{'action':'next'}),
 ('ตั้งเวลา 5 นาที','set_timer',{'duration':'5 minutes'}),
 ('ปิด Chrome','quit_app',{'name':'Google Chrome'}),
 ('เปิด example.com','open_url',{'url':'example.com'}),
])
def test_commands(text,tool,args):
 assert thai.parse(normalise_command(text)) == [(tool,args)]

def test_compound_and_followup():
 assert len(thai.parse(normalise_command('เปิด Google Chrome แล้วเข้า YouTube Music ให้หน่อย'))) == 2
 assert thai.parse(normalise_command('เปิด YouTube Music แล้วเล่นเพลง Bruno Mars')) is None
 assert thai.parse('ปิดแอปนั้น', 'Google Chrome') == [('quit_app',{'name':'Google Chrome'})]
 assert thai.parse('ลดเสียงเหลือ 130 เปอร์เซ็นต์') is None
 assert 'ตอนนี้' in thai.clock_reply('ตอนนี้กี่โมงแล้ว')

def test_unicode_search():
 from urllib.parse import unquote
 plan=thai.parse('ค้นหาข่าว NVIDIA ล่าสุด')
 assert 'ข่าว NVIDIA ล่าสุด' in unquote(plan[0][1]['url'])

@pytest.mark.parametrize('text,value',[('ยืนยันครับ',True),('ยกเลิก',False),('ไม่ครับ',False),('yes',True),('no',False),('ยืนยันหรือยัง',None)])
def test_confirm(text,value):
 assert classify_confirmation(text) is value

def test_prompt_and_persistence(tmp_path):
 c=Config(tmp_path/'config.json'); c.set('stt.language','auto'); c.set('voice.rate',160)
 assert Config(c.path).get('voice.rate')==160
 p=build_system_prompt(c,None)
 assert 'conversational Thai' in p
 assert p==build_system_prompt(c,None)
 assert normalise_command('Hey Jarvis, please open Chrome for me')=='open chrome'

def test_no_false_success():
 assert 'ไม่สำเร็จ' in thai.localise_result('open_app',{},'Error: permission denied')
 s='Chrome was asked to quit but is still open, probably showing an unsaved-changes dialog.'
 assert thai.localise_result('quit_app',{},s)==s

def test_whisper_options_and_silence(tmp_path):
 c=Config(tmp_path/'config.json'); stt=Transcriber(c)
 stt._model=SimpleNamespace(transcribe=Mock(return_value={'segments':[{'text':'เปิด Chrome ครับ','no_speech_prob':.01,'avg_logprob':-.2,'compression_ratio':1}]}))
 assert stt.transcribe_sync(np.zeros(16000,dtype=np.float32))==''
 assert not stt._model.transcribe.called
 assert stt.transcribe_sync(np.ones(16000,dtype=np.float32)*.1)=='เปิด Chrome ครับ'
 kw=stt._model.transcribe.call_args.kwargs
 assert kw['language'] is None and not kw['condition_on_previous_text']
 c.set('stt.language','th'); stt.transcribe_sync(np.ones(16000,dtype=np.float32)*.1)
 assert stt._model.transcribe.call_args.kwargs['language']=='th'
 stt._model.transcribe.return_value={'segments':[{'text':'hallucination','no_speech_prob':.9}]}
 assert stt.transcribe_sync(np.ones(16000,dtype=np.float32)*.1)==''
 stt.close()

def test_parakeet_fallback(tmp_path):
 c=Config(tmp_path/'config.json');c.set('stt.engine','parakeet');c.set('stt.model','mlx-community/parakeet-tdt-0.6b-v3')
 stt=Transcriber(c);stt._mx=SimpleNamespace(array=lambda x:x);stt._get_logmel=lambda a,b:a
 stt._model=SimpleNamespace(preprocessor_config={},generate=lambda x:[SimpleNamespace(text='open Chrome')])
 assert stt.transcribe_sync(np.ones(16000)*.1)=='open Chrome'
 stt.close()

@pytest.mark.asyncio
async def test_dispatch_and_safety(tmp_path,monkeypatch):
 load_all(); cfg=Config(tmp_path/'config.json')
 ctx=SimpleNamespace(cfg=cfg,confirm=AsyncMock(return_value=False))
 h=AsyncMock(return_value='should not run'); monkeypatch.setattr(REGISTRY['run_shell'],'handler',h)
 cfg.set('safety.confirm_risky',False)
 assert (await execute(ctx,'run_shell',{'command':'echo harmless'})).startswith('Cancelled')
 h.assert_not_called()
 assert ctx.confirm.called
 ctx.confirm.reset_mock(); cfg.set('safety.confirm_risky',True)
 h=AsyncMock();monkeypatch.setattr(REGISTRY['reminders'],'handler',h)
 assert (await execute(ctx,'reminders',{'action':'create','text':'ทดสอบ','when':'tomorrow'})).startswith('Cancelled')
 h.assert_not_called()

@pytest.mark.asyncio
async def test_real_fast_dispatch_mocked_actions(monkeypatch):
 import jarvis.core as core
 a=object.__new__(Assistant)
 a.cfg=SimpleNamespace(get=lambda k,d=None:'youtube_music' if k=='media.preferred' else d)
 a.memory=a.scheduler=a.jobs=None;a._last_app='';a._record_fast=Mock();a._quick_reply=AsyncMock(side_effect=lambda t,s:t)
 fn=AsyncMock(side_effect=['Google Chrome is open.','เปิด YouTube Music ให้แล้วครับ','Volume 30 percent.'])
 monkeypatch.setattr(core,'execute',fn)
 reply=await a._fast_path('เปิด Google Chrome แล้วเข้า YouTube Music ให้หน่อย',False)
 assert 'YouTube Music' in reply and fn.await_count==2
 reply=await a._fast_path('ลดเสียงเหลือ 30 เปอร์เซ็นต์',False)
 assert reply=='ปรับเสียงเป็น 30 เปอร์เซ็นต์แล้วครับ'
 assert fn.call_args.args[1:] == ('system_control',{'action':'volume_set','value':30})

@pytest.mark.asyncio
async def test_youtube_open_and_search(monkeypatch):
 import jarvis.tools.youtube_music as yt
 run=AsyncMock(return_value=SimpleNamespace(check=lambda _:None));monkeypatch.setattr(yt,'run',run)
 result=await yt.youtube_music(None,'search','Bruno Mars')
 assert 'เลือกเพลง' in result and 'search?q=Bruno%20Mars' in run.call_args.args[0][-1]
 result=await yt.youtube_music(None,'liked')
 assert 'ลงชื่อเข้าใช้' in result and run.call_args.args[0][-1].endswith('list=LM')

@pytest.mark.asyncio
async def test_youtube_verification(monkeypatch):
 import jarvis.tools.youtube_music as yt
 from jarvis.sysutil import ToolError
 state={'paused':True,'time':0,'src':'song','ready':4,'title':'Song'}
 monkeypatch.setattr(yt,'_js',AsyncMock(return_value=state))
 # Actual waiting ensures a pending command cannot be reported as success.
 with pytest.raises(ToolError,match='ยืนยันผลไม่ได้'):
  await yt.youtube_music(None,'play')


def test_listener_followup_and_wake(tmp_path):
 from jarvis.listener import Listener
 c=Config(tmp_path/'config.json'); events=[]
 listener=Listener(c,None,lambda name,data:events.append((name,data)),lambda:False)
 assert c.get('listening.wake_word')=='hey_jarvis'
 listener.request_capture(followup=True)
 listener._process(np.zeros(1280,dtype=np.int16))
 assert listener.mode==Listener.CAPTURE and listener._is_followup
 assert events[0][0]=='capture_start'
 listener._speech_ms=0;listener._capture_deadline=0
 listener._capture_frame(np.zeros(1280,dtype=np.int16),1)
 assert events[-1][0]=='timeout'

def test_memory_normalisation_preserves_thai():
 from jarvis.learning import normalise
 assert normalise('เปิดเพลง') != normalise('ปิดเพลง')
 assert normalise('ฉันชื่อสมชาย')=='ฉันชื่อสมชาย'

def test_power_and_browser_confirmation():
 load_all()
 assert REGISTRY['system_control'].confirmation_for({'action':'shutdown'})
 assert REGISTRY['system_control'].confirmation_for({'action':'wifi_off'})
 assert not REGISTRY['system_control'].confirmation_for({'action':'volume_set','value':30})
 for spec in REGISTRY.values():
  if spec.risk and spec.name.startswith('chrome'):
   assert spec.confirmation_for({'action':'submit','target':'ยืนยันการโอน'})

def test_native_voice_locale_resolution(tmp_path):
 from jarvis.tts import Speaker
 s=Speaker(Config(tmp_path/'cfg.json'),None)
 s._native_voices=['Samantha (อังกฤษ (สหรัฐฯ))','Kanya']
 assert s._resolve_voice('Samantha')=='Samantha (อังกฤษ (สหรัฐฯ))'
 assert s._resolve_voice('Kanya (ไทย (ไทย))')=='Kanya'
 assert s._resolve_voice('Unknown') is None

def test_tts_interrupt_discards_inflight_audio(tmp_path):
 import threading,time
 from jarvis.tts import Speaker
 output=SimpleNamespace(stop=Mock(),play=Mock(),playing=False,sample_rate=24000)
 s=Speaker(Config(tmp_path/'cfg.json'),output)
 entered=threading.Event();release=threading.Event()
 def slow_synth(*a):
  entered.set();release.wait(2);return np.ones(2400,dtype=np.float32),24000
 s.synth=slow_synth
 worker=threading.Thread(target=s._worker,daemon=True);worker.start()
 s.say('ข้อความแรก');assert entered.wait(2)
 s.say('ข้อความที่สอง');s.stop();release.set()
 deadline=time.monotonic()+2
 while s.busy and time.monotonic()<deadline:time.sleep(.01)
 output.play.assert_not_called();output.stop.assert_called_once()
 s.close();worker.join(2)
 assert not worker.is_alive()

def test_listener_trims_silence(tmp_path):
 from jarvis.listener import Listener
 events=[];l=Listener(Config(tmp_path/'cfg.json'),None,lambda a,b:events.append((a,b)),lambda:False)
 l._buf=[np.ones(32000,dtype=np.int16)];l._silence_ms=1000
 l._finish_capture('silence')
 assert len(events[-1][1]['audio'])==20000

@pytest.mark.asyncio
async def test_control_api_rejects_web_origins():
 from aiohttp.test_utils import make_mocked_request
 from aiohttp import web
 from jarvis.server import local_only
 handler=AsyncMock(return_value='ok')
 request=make_mocked_request('POST','/api/command',headers={'Host':'127.0.0.1:8765','Origin':'https://example.com'})
 with pytest.raises(web.HTTPForbidden):await local_only(request,handler)
 handler.assert_not_called()
 request=make_mocked_request('POST','/api/command',headers={'Host':'127.0.0.1:8765'})
 assert await local_only(request,handler)=='ok'

def test_auto_language_retries_nonpreferred_language(tmp_path):
 c=Config(tmp_path/'config.json');stt=Transcriber(c)
 stt._model=SimpleNamespace(transcribe=Mock(side_effect=[{'language':'vi','segments':[{'text':'wrong language'}]}, {'language':'th','segments':[{'text':'ตอนนี้กี่โมงแล้ว'}]}]))
 assert stt.transcribe_sync(np.ones(16000,dtype=np.float32)*.1)=='ตอนนี้กี่โมงแล้ว'
 assert stt._model.transcribe.call_args.kwargs['language']=='th'
 stt.close()

def test_listener_can_shutdown_and_join(tmp_path):
 import queue
 from jarvis.listener import Listener
 l=Listener(Config(tmp_path/'config.json'),SimpleNamespace(frames=queue.Queue()),lambda *a:None,lambda:False)
 l.start();l.stop();l.join(2)
 assert not l.is_alive()

@pytest.mark.asyncio
async def test_warm_keeps_context_and_does_not_reset_resident_model(tmp_path):
 from jarvis.llm import OllamaClient
 c=Config(tmp_path/'config.json');llm=OllamaClient(c)
 response=SimpleNamespace(raise_for_status=Mock())
 client=SimpleNamespace(post=AsyncMock(return_value=response))
 llm.client=AsyncMock(return_value=client)
 await llm.warm();await llm.warm()
 assert client.post.await_count==1
 assert client.post.call_args.kwargs['json']['options']['num_ctx']==c.get('llm.num_ctx')

def test_live_volume_mishearing_is_unambiguous():
 assert thai.parse('รสเสียงเหลือ 30%')==[('system_control',{'action':'volume_set','value':30})]

@pytest.mark.parametrize('text,seconds',[('5 นาที',300),('๑ ชั่วโมง',3600),('หนึ่งชั่วโมงครึ่ง',5400),('เก้าสิบวินาที',90),('ยี่สิบเอ็ดนาที',1260),('ครึ่งชั่วโมง',1800)])
def test_thai_duration_tool_arguments(text,seconds):
 from jarvis.tools.timers import parse_duration
 assert parse_duration(text)==seconds

@pytest.mark.asyncio
async def test_timer_schedules_thai_duration(tmp_path):
 import time
 from jarvis.tools.timers import set_timer
 ctx=SimpleNamespace(cfg=Config(tmp_path/'config.json'),scheduler=SimpleNamespace(add=Mock()))
 before=time.time();reply=await set_timer(ctx,'หนึ่งชั่วโมง','ชา')
 args=ctx.scheduler.add.call_args.args
 assert args[0]=='timer' and before+3599 <= args[1] <= time.time()+3601
 assert 'ครบเวลา ชา แล้วครับ'==args[2]
 assert '1 ชั่วโมง' in reply

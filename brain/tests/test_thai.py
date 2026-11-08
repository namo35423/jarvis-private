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
 ('เล่นเพลง Bruno Mars','youtube_music',{'action':'play','query':'bruno mars'}),
 ('ค้นหาเพลง Bruno Mars','youtube_music',{'action':'search','query':'bruno mars'}),
 ('เปิดเพลง Shape of You','youtube_music',{'action':'play','query':'shape of you'}),
 ('เปิดเพลง Lemonade ของ Internet Money','youtube_music',{'action':'play','query':'lemonade','artist':'internet money'}),
 ('เปิดเล่นเพลง Shape of You ของ Ed Sheeran ให้หน่อย','youtube_music',{'action':'play','query':'shape of you','artist':'ed sheeran'}),
 ('เปิดเพียง Shape of You ดิ','youtube_music',{'action':'play','query':'shape of you'}),
 ('เปิดเพลง Lemonade จาก YouTube Music','youtube_music',{'action':'play','query':'lemonade'}),
 ('เปิดเพลงที่ชอบ','youtube_music',{'action':'liked'}),
 ('อยากฟัง Perfect ของ Ed Sheeran อ่ะ','youtube_music',{'action':'play','query':'perfect','artist':'ed sheeran'}),
 ('ขอฟังเพลง Lemonade หน่อย','youtube_music',{'action':'play','query':'lemonade'}),
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


# ---- play a named song, LINE routing, skill-cache guard -------------------------

def test_song_title_artist_and_choice():
 import jarvis.tools.youtube_music as yt
 assert yt.split_title_artist('Shape of You ของ Ed Sheeran') == ('Shape of You','Ed Sheeran')
 assert yt.split_title_artist('Lemonade Internet Money','Internet Money') == ('Lemonade','Internet Money')
 items=[{'id':'a','title':'Shape of You (Cover)','sub':'เพลง • Some Cover • 4:00','shelf':'','top':True},
        {'id':'b','title':'Shape of You','sub':'Ed Sheeran • ÷ (Deluxe) • 3:54','shelf':'เพลง','top':False},
        {'id':'c','title':'Shape of You (Official Video)','sub':'วิดีโอ • Ed Sheeran • 6B','shelf':'','top':False}]
 assert yt.choose_result(items,'shape of you')['id']=='b'
 assert yt.choose_result(items,'shape of you','ed sheeran')['id']=='b'
 # Artist named but no result credits them: nothing qualifies, nothing plays.
 assert yt.choose_result(items,'shape of you','internet money') is None
 assert yt.choose_result([{'id':'e','title':'Shape of You','sub':'ตอน • Podcast','shelf':''}],'shape of you') is None

def _ytm_fakes(monkeypatch, player_states, items):
 import jarvis.tools.youtube_music as yt
 nav=AsyncMock(); monkeypatch.setattr(yt,'_navigate',nav)
 monkeypatch.setattr(yt.asyncio,'sleep',AsyncMock())
 states=iter(player_states)
 async def js_raw(code):
  if code==yt.RESULTS: return {'q':'Shape of You Ed Sheeran','items':items}
  if code==yt.PLAYER: return next(states, player_states[-1])
  return {'ok':True}
 monkeypatch.setattr(yt,'_js_raw',js_raw)
 return yt, nav

ITEMS=[{'id':'vid1','title':'Shape of You','sub':'Ed Sheeran • ÷ • 3:54','shelf':'Songs','top':True}]

@pytest.mark.asyncio
async def test_play_named_song_verifies_the_track(monkeypatch):
 old={'vid':'old','paused':False,'time':50,'ready':4,'title':'เจ้าพ่อเสียงหาย','artist':'x','ad':False}
 new=lambda t:{'vid':'vid1','paused':False,'time':t,'ready':4,'title':'Shape of You','artist':'Ed Sheeran','ad':False}
 yt,nav=_ytm_fakes(monkeypatch,[old,old,new(0.2),new(0.6)],ITEMS)
 result=await yt.youtube_music(None,'play','Shape of You','Ed Sheeran')
 assert result=='กำลังเล่น Shape of You ของ Ed Sheeran ครับ'
 assert 'search?q=Shape%20of%20You%20Ed%20Sheeran' in nav.call_args_list[0].args[0]

@pytest.mark.asyncio
async def test_play_named_song_never_claims_the_old_track(monkeypatch):
 from jarvis.sysutil import ToolError
 old={'vid':'old','paused':False,'time':50,'ready':4,'title':'เจ้าพ่อเสียงหาย','artist':'x','ad':False}
 yt,_=_ytm_fakes(monkeypatch,[old],ITEMS)
 with pytest.raises(ToolError,match='ยังยืนยันไม่ได้|ไม่เปลี่ยนเพลง'):
  await yt.youtube_music(None,'play','Shape of You ของ Ed Sheeran')

@pytest.mark.asyncio
async def test_play_named_song_reports_blocked_autoplay(monkeypatch):
 from jarvis.sysutil import ToolError
 paused={'vid':'vid1','paused':True,'time':0,'ready':4,'title':'Shape of You','artist':'Ed Sheeran','ad':False}
 yt,_=_ytm_fakes(monkeypatch,[paused],ITEMS)
 with pytest.raises(ToolError,match='ยังไม่เริ่มเล่น'):
  await yt.youtube_music(None,'play_query','Shape of You Ed Sheeran')

@pytest.mark.asyncio
async def test_play_without_query_still_resumes(monkeypatch):
 import jarvis.tools.youtube_music as yt
 seq=iter([{'paused':True,'time':5,'src':'s','ready':4,'title':'Song'},{'ok':True},{'paused':False,'time':6,'src':'s','ready':4,'title':'Song'}])
 monkeypatch.setattr(yt,'_js',AsyncMock(side_effect=lambda code: next(seq)))
 monkeypatch.setattr(yt.asyncio,'sleep',AsyncMock())
 assert await yt.youtube_music(None,'play')=='เล่นเพลงต่อแล้วครับ'

@pytest.mark.parametrize('text,expected',[
 ('ส่งข้อความถึง Somchai ผ่าน line ว่า  Hello world',('Somchai','Hello world')),
 ('ช่วยส่งไลน์หา Somchai ว่า กินข้าวยัง',('Somchai','กินข้าวยัง')),
 ('Hey Jarvis ส่ง LINE ถึง Somchai ว่า Hello World',('Somchai','Hello World')),
 ('ส่งข้อความหา Somchai ทางไลน์ว่า พรุ่งนี้เจอกัน',('Somchai','พรุ่งนี้เจอกัน')),
 ('ส่งข้อความหา Somchai ว่า hi',None),          # no app named: not LINE
 ('เปิด LINE',None),
])
def test_line_message_parsing(text,expected):
 assert thai.parse_line(text)==expected

def test_line_chat_matching_is_exact():
 from jarvis.tools.line import match_chats
 labels=['Somchai, เวลาโทร : 09:15, 1 มกราคม','Somchai Team, 3 คน, hi','Keep Memo, ส่งไฟล์แล้ว']
 assert match_chats(labels,'somchai')==(['Somchai, เวลาโทร : 09:15, 1 มกราคม'],['Somchai Team'])
 assert match_chats(labels,'somc')==([],['Somchai','Somchai Team'])

def test_line_is_confirmed_and_never_cached():
 from jarvis.tools import CONFIRM_ONLY
 from jarvis.learning import UNCACHEABLE
 load_all()
 assert 'line_send' in REGISTRY and 'line_send' in CONFIRM_ONLY and 'line_send' in UNCACHEABLE
 assert 'ไลน์' in REGISTRY['line_send'].confirmation_for({'recipient':'Somchai','text':'hi'})
 assert 'LINE' in REGISTRY['messages_send'].description  # tells the model Messages is not LINE

def test_skill_cache_only_replays_what_was_said():
 from jarvis.learning import plan_fits_request
 assert not plan_fits_request('เปิดดิ',[{'name':'youtube_music','args':{'action':'play','query':'Shape of You'}}])
 assert not plan_fits_request('ยังไม่เห็นเปิดเลย',[{'name':'open_app','args':{'name':'VS Code'}}])
 assert plan_fits_request('quit vscode',[{'name':'quit_app','args':{'name':'vscode'}}])
 assert plan_fits_request('เปิด YouTube Music',[{'name':'youtube_music','args':{'action':'open'}}])

@pytest.mark.asyncio
async def test_fast_path_routes_line_and_english_song(monkeypatch):
 import jarvis.core as core
 calls=[]
 async def fake_execute(ctx,name,args):
  calls.append((name,args)); return 'ok'
 monkeypatch.setattr(core,'execute',fake_execute)
 a=Assistant.__new__(Assistant)
 a.cfg=SimpleNamespace(get=lambda k,d=None:'youtube_music' if k=='media.preferred' else d)
 a.memory=Mock(); a.scheduler=a.jobs=None; a._last_app=''; a._record_fast=Mock(); a._quick_reply=AsyncMock(side_effect=lambda t,s:t)
 await a._fast_path('ส่งข้อความถึง Somchai ผ่าน LINE ว่า Hello World',False)
 await a._fast_path('play shape of you by ed sheeran',False)
 await a._fast_path('play',False)
 assert calls[0]==('line_send',{'recipient':'Somchai','text':'Hello World'})
 assert calls[1]==('youtube_music',{'action':'play','query':'shape of you','artist':'ed sheeran'})
 assert calls[2]==('media_control',{'action':'play'})

def test_player_match_by_id_or_title_and_artist():
 import jarvis.tools.youtube_music as yt
 pick={'id':'vid1','title':'Shape of You','sub':'เพลง • Ed Sheeran • 3:54'}
 assert yt._is_pick({'vid':'vid1','title':'x','artist':'y'},pick)
 assert yt._is_pick({'vid':'twin','title':'Shape of You','artist':'Ed Sheeran'},pick)
 assert not yt._is_pick({'vid':'old','title':'เจ้าพ่อเสียงหาย','artist':'x'},pick)
 assert not yt._is_pick({'vid':'cover','title':'Shape of You','artist':'Some Cover Band'},pick)

def test_player_match_tolerates_featuring_suffix():
 import jarvis.tools.youtube_music as yt
 pick={'id':'SfLfO8vBQk4','title':'Lemonade (ร่วมกับ NAV)','sub':'เพลง • Internet Money, Gunna และ Don Toliver • 3:16'}
 assert yt._is_pick({'vid':'','title':'Lemonade','artist':'Internet Money Gunna และDon Toliver'},pick)
 assert not yt._is_pick({'vid':'','title':'Lemonade','artist':'Beyoncé'},pick)
 assert not yt._is_pick({'vid':'','title':'Shape of You','artist':'Ed Sheeran'},pick)

def test_artist_must_be_credited_not_just_in_title():
 import jarvis.tools.youtube_music as yt
 items=[{'id':'mix','title':'Ed Sheeran - Shape Of You (Lyrics) | Taylor Swift (Mix)','sub':'วิดีโอ • Monalisa Music • 1M','shelf':''},
        {'id':'b','title':'Shape of You','sub':'เพลง • Ed Sheeran • 3:54','shelf':''}]
 assert yt.choose_result(items,'shape of you','taylor swift') is None
 assert yt.choose_result(items,'shape of you','ed sheeran')['id']=='b'

@pytest.mark.asyncio
async def test_osascript_timeout_is_not_remembered_as_denial(monkeypatch):
 from jarvis import sysutil
 from jarvis.sysutil import ToolError
 async def slow(*a,**k): raise ToolError('timed out')
 monkeypatch.setattr(sysutil,'run',slow)
 with pytest.raises(ToolError,match='did not respond'):
  await sysutil.osascript('tell application "Google Chrome" to get URL of tabs of windows')
 assert not sysutil.automation_denied('Google Chrome')

def test_title_must_match_even_when_artist_does():
 import jarvis.tools.youtube_music as yt
 items=[{'id':'eh','title':'Everything Has Changed (ร่วมกับ Ed Sheeran)','sub':'เพลง • Taylor Swift • 4:05','shelf':''}]
 assert yt.choose_result(items,'shape of you','taylor swift') is None
 assert yt.choose_result([{'id':'so','title':'Shake It Off','sub':'เพลง • Taylor Swift','shelf':''}],'shape of you','taylor swift') is None
 assert yt.choose_result([{'id':'s','title':'Shape of You','sub':'เพลง • Ed Sheeran','shelf':''}],'shape of u')['id']=='s'

@pytest.mark.asyncio
async def test_claimed_action_without_tool_is_not_reported():
 import jarvis.core as core
 assert core._CLAIMS_ACTION.search('กำลังเล่น Perfect ของ Ed Sheeran ครับ')
 assert core._CLAIMS_ACTION.search('ส่งไลน์หา Somchai แล้วครับ')
 assert not core._CLAIMS_ACTION.search('อยากให้เปิดเพลงอะไรครับ')

class _FakeLine:
 """Stands in for jarvis.tools.line._UI: a chat list where far rows are unnamed until searched."""
 def __init__(self, visible, searchable):
  self.visible, self.searchable, self.search = visible, searchable, ''
  self.field = object()
 def main_parts(self): return ('list', 'group')
 def rows(self, _):
  names = [n for n in self.searchable if self.search.casefold() in n.casefold()] if self.search else self.visible
  return [(n + ', preview, 7 พฤศจิกายน', n) for n in names]
 def kids(self, _): return [self.field]
 def role(self, el): return 'AXTextField' if el is self.field else ''
 def value(self, el): return self.search
 def set(self, el, attr, value):
  self.search = value; return True

def test_line_lookup_waits_then_searches_and_clears(monkeypatch):
 import jarvis.tools.line as line
 from jarvis.sysutil import ToolError
 monkeypatch.setattr(line.time, 'sleep', lambda s: None)
 ui = _FakeLine(['Somchai', 'Keep Memo'], ['Somchai', 'Keep Memo', 'Old Classmate'])
 assert line.find_chat(ui, 'somchai')[1:] == ('Somchai', None)
 target, name, searched = line.find_chat(ui, 'Old Classmate')
 assert name == 'Old Classmate' and searched is ui.field and ui.search == 'Old Classmate'
 ui.search = ''
 with pytest.raises(ToolError, match='ไม่พบแชท'):
  line.find_chat(ui, 'Nobody Here')
 assert ui.search == ''  # the search box is put back after a miss


# ---- everyday commands: routing, Thai clock, wording -----------------------------
from datetime import datetime as _dt
from jarvis import thai_time

@pytest.mark.parametrize('text,tool,check', [
 ('ข้างนอกร้อนไหม','get_weather',{'when':'now'}),
 ('พรุ่งนี้ฝนจะตกไหม','get_weather',{'when':'tomorrow'}),
 ('อากาศที่เชียงใหม่วันนี้เป็นไง','get_weather',{'location':'เชียงใหม่','when':'today'}),
 ('ตั้งเวลาครึ่งชั่วโมง','set_timer',{'duration':'30 minutes'}),
 ('อีก 5 นาทีเตือนให้ปิดแก๊สด้วย','set_timer',{'duration':'5 minutes','label':'ปิดแก๊ส'}),
 ('ตั้งเวลาเหลืออีกเท่าไหร่','timers',{'action':'list'}),
 ('ยกเลิกตั้งเวลา','timers',{'action':'cancel_all'}),
 ('พรุ่งนี้มีนัดอะไรบ้าง','calendar',{'action':'list','day':'tomorrow'}),
 ('วันนี้มีอะไรต้องทำบ้าง','calendar',{'action':'list','day':'today'}),
 ('เบาเสียงลงหน่อย','system_control',{'action':'volume_down'}),
 ('ตอนนี้เสียงดังเท่าไหร่','system_control',{'action':'volume_status'}),
 ('แบตเหลือกี่เปอร์เซ็นต์','system_control',{'action':'battery'}),
 ('พื้นที่ในเครื่องเหลือเท่าไหร่','system_control',{'action':'disk_space'}),
 ('เปิดโหมดมืด','system_control',{'action':'dark_mode'}),
 ('ล็อกหน้าจอ','system_control',{'action':'lock_screen'}),
 ('เช็คไวไฟหน่อย','system_control',{'action':'wifi_status'}),
 ('เปิดไลน์','open_app',{'name':'LINE'}),
 ('เปิดแอปปฏิทิน','open_app',{'name':'Calendar'}),
 ('เปิดเฟซบุ๊ก','open_url',{'url':'facebook.com'}),
 ('เข้าเว็บพันทิป','open_url',{'url':'pantip.com'}),
 ('100 ดอลลาร์เป็นกี่บาท','convert_currency',{'from_currency':'USD','to_currency':'THB','amount':100.0}),
 ('1000 เยนกี่บาท','convert_currency',{'from_currency':'JPY','amount':1000.0}),
 ('ราคาทองวันนี้กี่บาท','web_search',{}),
 ('จำไว้ว่าฉันชอบกาแฟเย็น','remember',{'fact':'ฉันชอบกาแฟเย็น'}),
 ('จดโน้ตว่าพรุ่งนี้ซื้อนม','notes',{'action':'create','body':'พรุ่งนี้ซื้อนม'}),
 ('เพลงที่เล่นอยู่ชื่ออะไร','youtube_music',{'action':'now_playing'}),
 ('ปิดเพลง','youtube_music',{'action':'pause'}),
 ('เพลงก่อนหน้า','youtube_music',{'action':'previous'}),
 ('เปิดเพลงของ Ed Sheeran','youtube_music',{'action':'play','artist':'ed sheeran'}),
 ('เปิดเพลงสบายๆ ฟังหน่อย','youtube_music',{'action':'play','query':'สบายๆ'}),
 ('ปิดแท็บนี้','chrome_tabs',{'action':'close'}),
 ('หาไฟล์ PDF ใบเสร็จ','find_files',{'query':'ใบเสร็จ','kind':'pdf'}),
 ('หารูปแมว','find_files',{'kind':'image'}),
 ('สรุปหน้าเว็บนี้ให้หน่อย','chrome_read_page',{}),
 ('บนจอมีอะไรอยู่','screen_look',{'mode':'describe'}),
 ('แคปหน้าจอให้หน่อย','screen_look',{'mode':'save'}),
 ('สรุปเช้านี้ให้หน่อย','daily_briefing',{}),
])
def test_everyday_routing(text,tool,check):
 plan=thai.parse(normalise_command(text))
 assert plan and plan[0][0]==tool, plan
 for k,v in check.items():
  assert plan[0][1].get(k)==v, plan

@pytest.mark.parametrize('text', ['ผักดองราคาเท่าไหร่','ขอวอนหน่อย','บันทึกไฟล์นี้','ตั้งเวลา','สวัสดีครับ','ฉันกินข้าวแล้วและนอนแล้ว'])
def test_everyday_routing_leaves_these_alone(text):
 plan=thai.parse(normalise_command(text))
 assert not plan or plan[0][0] not in {'convert_currency','notes','set_timer'}

@pytest.mark.parametrize('text,alarm,hm',[
 ('สองทุ่ม',False,(20,0)),('บ่ายสองโมง',False,(14,0)),('ตีสามครึ่ง',False,(3,30)),('เจ็ดโมงเช้า',False,(7,0)),
 ('หกโมง',True,(6,0)),('หกโมง',False,(18,0)),('สี่โมง',False,(16,0)),('ห้าโมงเย็น',False,(17,0)),('เที่ยงคืน',False,(0,0)),
 ('ทุ่มครึ่ง',False,(19,30)),('สิบโมงครึ่ง',False,(10,30)),('20.30 น.',False,(20,30)),('17 นาฬิกา 15 นาที',False,(17,15)),
])
def test_thai_six_hour_clock(text,alarm,hm):
 assert thai_time.find_time(text,alarm)[:2]==hm

def test_thai_time_resolution_and_model_text():
 now=_dt(2026,11,8,21,0)
 assert thai_time.resolve('เตือนให้กินยาตอนสองทุ่ม',now=now)[0]==_dt(2026,11,9,20,0)  # already past today
 assert thai_time.resolve('ลงนัดหมอฟันวันศุกร์บ่ายสองโมง',now=now)[0]==_dt(2026,11,13,14,0)
 assert thai_time.find_time('ตั้งเวลา 2 ชั่วโมง') is None
 assert thai_time.explicit_times('เตือนให้กินยาตอนสองทุ่ม')=='เตือนให้กินยาตอน 20:00'

def test_reminder_and_alarm_plans():
 r=thai.parse(normalise_command('พรุ่งนี้เก้าโมงเตือนให้โทรหาแม่'))[0]
 assert r[0]=='reminders' and r[1]['text']=='โทรหาแม่' and r[1]['when'].endswith('09:00')
 a=thai.parse(normalise_command('ตั้งปลุกตอน 7 โมงเช้า'))[0]
 assert a[0]=='set_timer' and a[1]['label'].startswith('ปลุก') and '07:00' in a[1]['label']
 c=thai.parse(normalise_command('ลงนัดหมอฟันวันศุกร์บ่ายสองโมง'))[0]
 assert c==('calendar',{'action':'create','title':'นัดหมอฟัน','start':c[1]['start']}) and c[1]['start'].endswith('14:00')

def test_data_questions_are_worded_after_the_tool():
 assert thai.needs_wording([('find_files',{})]) and not thai.needs_wording([('get_weather',{})]) and thai.needs_wording([('calendar',{'action':'list'})])
 assert not thai.needs_wording([('calendar',{'action':'create'})]) and not thai.needs_wording([('screen_look',{'mode':'save'})])

@pytest.mark.parametrize('name,args,raw,thai_text',[
 ('system_control',{},'Battery 85 percent, charging, 1 hour 5 minutes to full.','แบตเตอรี่เหลือ 85 เปอร์เซ็นต์ กำลังชาร์จ อีก 1 ชั่วโมง 5 นาที จะเต็มครับ'),
 ('system_control',{},'Battery 60 percent, 3 hours remaining.','แบตเตอรี่เหลือ 60 เปอร์เซ็นต์ ใช้ได้อีกประมาณ 3 ชั่วโมงครับ'),
 ('system_control',{},'Disk 272 GB free of 995 GB.','พื้นที่ว่างเหลือ 272 GB จากทั้งหมด 995 GB ครับ'),
 ('system_control',{},'Volume is 40 percent but muted.','ตอนนี้เสียงอยู่ที่ 40 เปอร์เซ็นต์ แต่ปิดเสียงอยู่ครับ'),
 ('system_control',{},'Wi-Fi is on, connected to Home.','Wi-Fi เปิดอยู่ เชื่อมต่อกับ Home ครับ'),
 ('timers',{},'Cancelled 2 timers.','ยกเลิกการตั้งเวลา 2 รายการแล้วครับ'),
 ('chrome_tabs',{'action':'close'},'Closed the current tab.','ปิดแท็บแล้วครับ'),
 ('reminders',{'text':'กินยา','when':'2030-01-01 20:00'},'Reminder set: กินยา, on Tuesday the 1 at 8:00 pm.','ตั้งเตือน กินยา วันอังคารที่ 1 เวลา 20:00 แล้วครับ'),
])
def test_results_are_spoken_in_thai(name,args,raw,thai_text):
 assert thai.localise_result(name,args,raw)==thai_text

@pytest.mark.asyncio
async def test_currency_tool(monkeypatch):
 import jarvis.tools.money as money
 class R:
  def json(self): return {'result':'success','rates':{'THB':33.5},'time_last_update_utc':'Sat, 07 Nov 2026 00:00:01 +0000'}
 class C:
  async def __aenter__(self): return self
  async def __aexit__(self,*a): return False
  async def get(self,url): return R()
 monkeypatch.setattr(money.httpx,'AsyncClient',lambda **k: C())
 assert await money.convert_currency(None,'USD','THB',100)=='100 ดอลลาร์สหรัฐ เท่ากับประมาณ 3,350 บาทครับ ตามอัตราวันที่ 7 พฤศจิกายน'

@pytest.mark.asyncio
async def test_forced_tool_runs_before_the_model(monkeypatch):
 import jarvis.core as core
 a=object.__new__(Assistant)
 a.cfg=SimpleNamespace(get=lambda k,d=None:'youtube_music' if k=='media.preferred' else d)
 a.memory=Mock(); a.scheduler=a.jobs=None; a._last_app=''; a._record_fast=Mock()
 a._quick_reply=AsyncMock(side_effect=lambda t,s:t); a._forced_plan=None
 assert await a._fast_path('สรุปหน้าเว็บนี้ให้หน่อย',False) is None
 assert a._forced_plan[0][0]=='chrome_read_page'

def test_claim_guard_catches_open_but_not_questions():
 import jarvis.core as core
 assert core._CLAIMS_ACTION.search('Finder ได้เปิดแล้วครับ') and not core._ASKS.search('Finder ได้เปิดแล้วครับ')
 assert core._ASKS.search('ต้องการให้ตั้งเวลากี่นาทีครับ')

def test_invented_numbers_from_search_are_caught():
 import jarvis.core as core
 snippets='ราคาทองวันนี้ 96.5% (บาททองคำ) - goldcalctool.com - เช็คราคาทองวันนี้ ... ปรับตัวเพิ่ม 450 บาท'
 assert core._ungrounded_numbers('ราคาทองวันนี้อยู่ที่ประมาณ 29,500 บาทต่อกิโลกรัม',snippets)==['29,500']
 assert core._ungrounded_numbers('วันนี้ทองปรับขึ้น 450 บาท ปี 2569',snippets)==[]

def test_timer_list_in_thai():
 assert thai.localise_result('timers',{},'2 timers: ปิดแก๊ส, 4 minutes left; 3 hours 59 minutes left.')=='มีตั้งเวลา 2 รายการ: ปิดแก๊ส เหลืออีก 4 นาที และ เหลืออีก 3 ชั่วโมง 59 นาที ครับ'
 assert thai.localise_result('system_control',{},'Wi-Fi is on and connected.')=='Wi-Fi เปิดอยู่และเชื่อมต่อแล้วครับ'


@pytest.mark.asyncio
async def test_location_switch_beats_cached_coordinates():
    from jarvis.tools import web

    class Cfg(dict):
        def get(self, k, d=None): return super().get(k, d)
        def set(self, k, v): self[k] = v

    # Coordinates left over from an old IP lookup (Singapore) must not win over the switch.
    cfg = Cfg({"language": "th", "location.city": "Bangkok", "location.lat": 1.35, "location.lon": 103.7})
    ctx = type("C", (), {"cfg": cfg})()
    place = await web._resolve_place(ctx, None, "")
    assert (place["city"], place["timezone"]) == ("กรุงเทพฯ", "Asia/Bangkok")
    cfg["location.city"] = "Hong Kong"
    assert (await web._resolve_place(ctx, None, ""))["city"] == "ฮ่องกง"
    # Naming either city in the request needs no network lookup either.
    assert (await web._resolve_place(ctx, None, "กรุงเทพ"))["lat"] == web.PLACES["bangkok"]["lat"]


def test_weather_is_said_in_thai():
    from jarvis.tools import web
    data = {
        "current": {"time": "2026-11-08T10:00", "temperature_2m": 25.2, "apparent_temperature": 29.6,
                    "weather_code": 2, "wind_speed_10m": 9},
        "hourly": {"time": [f"2026-11-08T{h:02d}:00" for h in range(24)],
                   "precipitation_probability": [0] * 15 + [70] + [0] * 8, "weather_code": [2] * 15 + [61] + [2] * 8},
        "daily": {"time": ["2026-11-08", "2026-11-09", "2026-11-10"], "weather_code": [2, 51, 0],
                  "temperature_2m_max": [27.4, 24.1, 26], "temperature_2m_min": [23, 16.4, 18],
                  "precipitation_probability_max": [70, 61, 0], "wind_speed_10m_max": [10, 12, 40]},
    }
    assert web._weather_th(data, "now", "ฮ่องกง", False) == (
        "ตอนนี้ที่ฮ่องกง 25 องศา มีเมฆบางส่วน รู้สึกเหมือน 30 องศา วันนี้สูงสุด 27 ต่ำสุด 23 องศา "
        "อาจมีฝนตกเล็กน้อย ราวบ่าย 3 โมงครับ")
    assert web._weather_th(data, "tomorrow", "เชียงใหม่, จังหวัดเชียงใหม่", False) == (
        "พรุ่งนี้ที่เชียงใหม่ ฝนปรอยๆ สูงสุด 24 ต่ำสุด 16 องศา โอกาสฝนตก 61 เปอร์เซ็นต์ครับ")
    data["daily"]["precipitation_probability_max"][1] = 10
    assert web._weather_th(data, "tomorrow", "ฮ่องกง", False) == "พรุ่งนี้ที่ฮ่องกง ฝนปรอยๆ สูงสุด 24 ต่ำสุด 16 องศาครับ"
    assert web._weather_th(data, "week", "กรุงเทพฯ", False) == (
        "สัปดาห์นี้ที่กรุงเทพฯ สูงสุด 24 ถึง 27 ต่ำสุด 16 ถึง 23 องศา มีฝนวันจันทร์ครับ")
    assert [web._clock_th(f"2026-11-08T{h:02d}:00") for h in (0, 3, 8, 12, 13, 14, 17, 20)] == [
        "เที่ยงคืน", "ตี 3", "8 โมงเช้า", "เที่ยง", "บ่ายโมง", "บ่าย 2 โมง", "5 โมงเย็น", "2 ทุ่ม"]
    assert thai.localise_result("get_weather", {}, "Error: I could not find a place called 'ฮ่องกงใต้'.") == \
        "หาสถานที่ชื่อ ฮ่องกงใต้ ไม่เจอครับ"


@pytest.mark.asyncio
async def test_commands_wait_for_startup_instead_of_failing():
    import asyncio
    a = object.__new__(Assistant)
    a._started = asyncio.Event(); a._pending_confirm = None
    a.broadcast = Mock(); a.memory = Mock(); a.cfg = SimpleNamespace(get=lambda k, d=None: d)
    a._fast_path = AsyncMock(return_value="ok")
    task = asyncio.create_task(a.handle_input("อากาศเป็นไง", source="api", speak=False))
    await asyncio.sleep(0.05)
    assert not task.done() and not a._fast_path.called   # tools are not loaded yet
    a._started.set()
    assert await asyncio.wait_for(task, 1) == "ok"

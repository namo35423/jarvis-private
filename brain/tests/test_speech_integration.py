"""Opt-in cached-model tests: JARVIS_TEST_SPEECH=1 HF_HUB_OFFLINE=1 pytest.

Controlled native-TTS audio, not a substitute for testing the user's real voice.
No microphone, playback, web access or tool side effects.
"""
import os
import numpy as np
import pytest
from jarvis.config import Config
from jarvis.core import normalise_command
from jarvis import thai
from jarvis.stt import Transcriber
from jarvis.tts import Speaker,_resample
pytestmark=pytest.mark.skipif(os.environ.get('JARVIS_TEST_SPEECH')!='1',reason='opt-in local model test')

@pytest.mark.asyncio
async def test_controlled_thai_mixed_and_english(tmp_path):
 cfg=Config(tmp_path/'config.json');speaker=Speaker(cfg,None);speaker.load_sync()
 stt=Transcriber(cfg)
 try:
  await stt.load()
  for text,expected in [
   ('เปิด Google Chrome แล้วเข้า YouTube Music ให้หน่อย',[('open_app',{'name':'Google Chrome'}),('youtube_music',{'action':'open'})]),
   ('ลดเสียงลงเหลือ 30 เปอร์เซ็นต์',[('system_control',{'action':'volume_set','value':30})]),
  ]:
   samples,sr=speaker.synth(text)
   assert len(samples)>1000 and np.abs(samples).max()>.01
   audio=np.concatenate([np.zeros(4000,dtype=np.float32),_resample(samples,sr,16000),np.zeros(4000,dtype=np.float32)])
   transcript=await stt.transcribe(audio)
   assert thai.parse(normalise_command(transcript))==expected,transcript
  samples,sr=speaker.synth('Jarvis, open my Python project')
  transcript=await stt.transcribe(_resample(samples,sr,16000))
  assert 'python project' in transcript.lower(),transcript
  assert await stt.transcribe(np.zeros(32000,dtype=np.float32))==''
 finally:
  stt.close();speaker.close()

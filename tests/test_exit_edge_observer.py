import asyncio
import json
from types import SimpleNamespace

import pytest

from bhiksha.ops import exit_edge_observer as observer


def test_watchdog_detects_stall_and_preserves_stage(tmp_path):
    now=[0.0]; exits=[]
    watchdog=observer.ObserverProgressWatchdog(tmp_path/'status.json',deadline=10,
        monotonic=lambda:now[0],terminate=exits.append)
    watchdog.advance('request_quotes')
    now[0]=9
    assert not watchdog.check()
    now[0]=11
    assert watchdog.check() and exits==[1]
    assert json.loads((tmp_path/'status.watchdog.json').read_text())['stage']=='request_quotes'


def test_watchdog_progress_resets_deadline(tmp_path):
    now=[0.0]
    watchdog=observer.ObserverProgressWatchdog(tmp_path/'status.json',deadline=10,monotonic=lambda:now[0])
    now[0]=9;watchdog.advance('poll_complete');now[0]=18
    assert not watchdog.check()


@pytest.mark.asyncio
async def test_quote_deadline_records_failure_and_allows_next_poll(tmp_path,monkeypatch):
    marker=tmp_path/'enabled';marker.touch()
    stop=asyncio.Event(); errors=[]; polls=[]
    class Recorder:
        def __init__(self,**kwargs): pass
        def start(self): pass
        def snapshot(self): return {'ready':True,'worker_alive':True}
        def refresh_active_from_store(self): pass
        def censor_expired_options(self,now): pass
        def active_option_symbols(self): return ('OPTION',)
        def record_observation_error(self,reason): errors.append(reason)
        def record_observation_poll(self,*args,**kwargs): polls.append(args);stop.set()
        def observe_quote(self,*args): pass
        def close(self,**kwargs): pass
    class Reader:
        calls=0
        async def quotes(self,symbols):
            self.calls+=1
            if self.calls==1: await asyncio.Event().wait()
            return {'OPTION':SimpleNamespace()}
        async def close(self): pass
    monkeypatch.setattr(observer,'ExitEdgeLiveRecorder',Recorder)
    monkeypatch.setattr(observer,'_regular_session',lambda now:True)
    monkeypatch.setattr(observer,'QUOTE_DEADLINE_SECONDS',0.01)
    reader=Reader()
    await asyncio.wait_for(observer.run_observer(db_path=tmp_path/'db',status_path=tmp_path/'status',
        enable_marker=marker,reader=reader,stop=stop,poll_seconds=1),timeout=4)
    assert errors==['TimeoutError'] and reader.calls==2 and len(polls)==1

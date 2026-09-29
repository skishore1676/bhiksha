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


def test_observer_owner_has_timely_io_and_existing_restart_policy(tmp_path):
    from bhiksha.ops.launchd_registry import ACTIVE_LAUNCHD_JOBS
    job=next(j for j in ACTIVE_LAUNCHD_JOBS if j.runner_job=='exit-edge-observer')
    plist=job.plist_payload(repo_root=tmp_path)
    assert plist['ProcessType']=='Standard'
    assert not plist.get('LowPriorityIO',False)
    assert plist['KeepAlive'] and plist['RunAtLoad']


def test_watchdog_detects_inflight_writer_without_failing_idle(tmp_path):
    health={'oldest_pending_write_seconds':0}
    exits=[]
    watchdog=observer.ObserverProgressWatchdog(tmp_path/'status.json',deadline=10,
        terminate=exits.append,writer_health=lambda:health)
    assert not watchdog.check()
    health['oldest_pending_write_seconds']=11
    watchdog.advance('poll_complete')
    assert watchdog.check() and exits==[1]
    assert json.loads((tmp_path/'status.watchdog.json').read_text())['stage']=='persist_queued_facts'


@pytest.mark.asyncio
async def test_offhours_heartbeat_without_quotes(tmp_path,monkeypatch):
    marker=tmp_path/'enabled';marker.touch()
    stop=asyncio.Event(); modes=[]
    class Recorder:
        def __init__(self,**kwargs): pass
        def start(self): pass
        def snapshot(self): return {'ready':True,'worker_alive':True}
        def refresh_active_from_store(self): pass
        def censor_expired_options(self,now): pass
        def active_option_symbols(self): return ('OPTION',)
        def heartbeat(self,*,mode): modes.append(mode);stop.set()
        def close(self,**kwargs): pass
    class Reader:
        async def quotes(self,symbols): raise AssertionError('offhours quote call')
        async def close(self): pass
    monkeypatch.setattr(observer,'ExitEdgeLiveRecorder',Recorder)
    monkeypatch.setattr(observer,'_regular_session',lambda now:False)
    await observer.run_observer(db_path=tmp_path/'db',status_path=tmp_path/'status',
        enable_marker=marker,reader=Reader(),stop=stop)
    assert modes==['idle_market_closed']


def test_registration_cursor_does_not_skip_concurrent_insert(tmp_path,monkeypatch):
    import sqlite3
    path=tmp_path/'events.db'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE events(id INTEGER PRIMARY KEY,event_type TEXT,payload TEXT)')
        db.execute("INSERT INTO events VALUES(1,'signal_outcome','{}')")
    original=observer.sqlite3.connect
    class Connection:
        def __enter__(self): self.conn=original(path);return self
        def __exit__(self,*args): self.conn.close()
        def execute(self,sql,args=()):
            result=self.conn.execute(sql,args)
            if 'MAX(id)' in sql:
                values=result.fetchall()
                with original(path) as writer:
                    writer.execute("INSERT OR IGNORE INTO events VALUES(2,'signal_outcome','{}')")
                return SimpleNamespace(fetchone=lambda:values[0])
            return result
    monkeypatch.setattr(observer.sqlite3,'connect',lambda *a,**k:Connection())
    monkeypatch.setattr(observer,'ProspectiveQuoteTapeRepository',lambda *a,**k:None)
    assert observer.recover_registration_intents(path,tmp_path/'edge',0)==1
    assert observer.recover_registration_intents(path,tmp_path/'edge',1)==2


def test_status_separates_idle_from_failed_evidence(tmp_path):
    from datetime import UTC, datetime
    from bhiksha.tools.launchd_status import _exit_edge_observer_status
    marker=tmp_path/'artifacts/playbook/runtime_flags/exit_edge_live_shadow.enabled'
    marker.parent.mkdir(parents=True);marker.touch()
    status=tmp_path/'artifacts/observations/exit_edge_live_status.json'
    status.parent.mkdir(parents=True)
    now=datetime.now(UTC)
    health={'updated_at':now.isoformat(),'role':'observer','ready':True,'worker_alive':True,
            'collection_state':'idle_market_closed','observation_polls':10}
    status.write_text(json.dumps(health))
    assert _exit_edge_observer_status(tmp_path,{'loaded':True},now)['ok']
    health['collection_state']='observing';status.write_text(json.dumps(health))
    result=_exit_edge_observer_status(tmp_path,{'loaded':True},now)
    assert not result['ok'] and result['status']=='observer_quote_evidence_stale'

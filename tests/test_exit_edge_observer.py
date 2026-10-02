import asyncio
import json
from datetime import UTC, datetime
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
        enable_marker=marker,reader=reader,stop=stop,poll_seconds=1,
        now_fn=lambda:datetime(2026,10,2,15,0,tzinfo=UTC)),timeout=4)
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
        def start(self): raise AssertionError('offhours database recovery')
        def snapshot(self): return {'ready':False,'worker_alive':False}
        def refresh_active_from_store(self): raise AssertionError('offhours scan')
        def censor_expired_options(self,now): raise AssertionError('offhours censor')
        def active_option_symbols(self): raise AssertionError('offhours evaluation')
        def heartbeat(self,*,mode): modes.append(mode);stop.set()
        def close(self,**kwargs): pass
    class Reader:
        async def quotes(self,symbols): raise AssertionError('offhours quote call')
        async def close(self): pass
    monkeypatch.setattr(observer,'ExitEdgeLiveRecorder',Recorder)
    monkeypatch.setattr(observer,'_regular_session',lambda now:False)
    await observer.run_observer(db_path=tmp_path/'db',status_path=tmp_path/'status',
        enable_marker=marker,reader=Reader(),stop=stop,
        now_fn=lambda:datetime(2026,10,3,15,0,tzinfo=UTC))
    assert modes==['idle_market_closed']


def test_registration_cursor_does_not_skip_concurrent_insert(tmp_path,monkeypatch):
    import sqlite3
    path=tmp_path/'events.db'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE events(id INTEGER PRIMARY KEY,event_type TEXT,payload TEXT)')
        db.execute("INSERT INTO events VALUES(1,'signal_outcome','{}')")
    original=observer.sqlite3.connect
    class Connection:
        def __init__(self): self.conn=original(path)
        def close(self): self.conn.close()
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


@pytest.mark.parametrize("retry_pending", [False, True])
def test_registration_scan_closes_source_connection_on_completion_and_retry(tmp_path, monkeypatch, retry_pending):
    import sqlite3
    path = tmp_path / "events.db"
    setup = sqlite3.connect(path)
    try:
        setup.execute("CREATE TABLE events(id INTEGER PRIMARY KEY,event_type TEXT,payload TEXT)")
        payload = {"attempt": {"trade_id": "pending"}, "cohort": None} if retry_pending else {}
        kind = "exit_edge_registration_intent" if retry_pending else "signal_outcome"
        setup.execute("INSERT INTO events VALUES(1,?,?)", (kind, json.dumps(payload)))
        setup.commit()
    finally:
        setup.close()
    opened = []
    original = sqlite3.connect

    def retain(*args, **kwargs):
        conn = original(*args, **kwargs)
        opened.append(conn)
        return conn

    monkeypatch.setattr(observer.sqlite3, "connect", retain)
    monkeypatch.setattr(observer.ProspectiveQuoteTapeRepository, "try_record_registration_attempt",
                        lambda *args, **kwargs: False)
    for _ in range(25):
        assert observer.recover_registration_intents(path, tmp_path / "edge", 0) == (0 if retry_pending else 1)
    assert len(opened) == 25
    try:
        for conn in opened:
            with pytest.raises(sqlite3.ProgrammingError, match="closed"):
                conn.execute("SELECT 1")
    finally:
        for conn in opened:
            conn.close()


@pytest.mark.parametrize('stamp,phase', [
    ('2026-10-02T13:19:59+00:00', 'closed'),
    ('2026-10-02T13:20:00+00:00', 'warmup'),
    ('2026-10-02T13:30:00+00:00', 'open'),
    ('2026-10-02T20:00:00+00:00', 'closed'),
    ('2026-10-03T15:00:00+00:00', 'closed'),  # Saturday
    ('2026-11-26T15:00:00+00:00', 'closed'),  # Thanksgiving
    ('2026-11-27T17:59:59+00:00', 'open'),
    ('2026-11-27T18:00:00+00:00', 'closed'),  # Early close
])
def test_observer_exchange_session_boundaries(stamp, phase):
    now = datetime.fromisoformat(stamp)
    assert observer._session_phase(now) == phase
    assert observer._regular_session(now) == (phase == 'open')


def test_idle_wait_is_lightweight_and_wakes_at_session_boundaries():
    assert observer._wait_seconds(datetime(2026,10,3,15,tzinfo=UTC), 'closed', 15) == 60
    assert observer._wait_seconds(datetime(2026,10,2,13,19,40,tzinfo=UTC), 'closed', 15) == 20
    assert observer._wait_seconds(datetime(2026,10,2,13,29,59,tzinfo=UTC), 'warmup', 15) == 1
    assert observer._wait_seconds(datetime(2026,11,27,17,59,59,tzinfo=UTC), 'open', 15) == 1


@pytest.mark.asyncio
async def test_warmup_collect_drain_and_idle_preserve_one_worker(tmp_path, monkeypatch):
    marker = tmp_path/'enabled'; marker.touch()
    stamps = [datetime.fromisoformat(s) for s in (
        '2026-11-27T14:19:50+00:00',  # Cold, premarket
        '2026-11-27T14:20:00+00:00',  # Recover 10 minutes before open
        '2026-11-27T14:30:00+00:00',  # Collect
        '2026-11-27T18:00:00+00:00',  # Drain at early close
        '2026-11-27T18:00:01+00:00',  # Idle with saved unfinished cohort
        '2026-11-28T15:00:00+00:00',  # Weekend, no additional scans
        '2026-11-30T14:20:00+00:00',  # Resume recovery using the same worker
    )]
    index = [0]; calls = []; modes = []
    class Stop:
        done = False
        def is_set(self): return self.done
        async def wait(self):
            if index[0] == 3: rec.pending = 0
            index[0] += 1
            if index[0] == len(stamps): self.done = True; index[0] -= 1
            await asyncio.sleep(0)
    stop = Stop()
    class Recorder:
        ready = False; pending = 0
        def __init__(self, **kwargs): pass
        def start(self): calls.append(('start',index[0])); self.ready=True
        def snapshot(self): return {'ready':self.ready,'worker_alive':self.ready,'pending_writes':self.pending}
        def refresh_active_from_store(self): calls.append(('refresh',index[0]))
        def censor_expired_options(self, now): pass
        def active_option_symbols(self): return ('OPTION',)
        def heartbeat(self, *, mode): modes.append((mode,index[0]))
        def observe_quote(self, *args): self.pending=1
        def record_observation_poll(self,*args,**kwargs): pass
        def record_observation_error(self,reason): raise AssertionError(reason)
        def close(self,**kwargs): calls.append(('close',index[0]))
    rec=Recorder()
    class Reader:
        async def quotes(self,symbols): calls.append(('quotes',index[0]));return {'OPTION':SimpleNamespace()}
        async def close(self): pass
    monkeypatch.setattr(observer,'ExitEdgeLiveRecorder',lambda **kw:rec)
    monkeypatch.setattr(observer,'recover_registration_intents',lambda *a:calls.append(('intents',index[0])) or 1)
    await observer.run_observer(db_path=tmp_path/'db',event_db_path=tmp_path/'events',
        status_path=tmp_path/'status',enable_marker=marker,reader=Reader(),stop=stop,
        now_fn=lambda:stamps[index[0]])
    assert [i for kind,i in calls if kind=='start'] == [1]
    assert [i for kind,i in calls if kind=='refresh'] == [1,2,6]
    assert [i for kind,i in calls if kind=='intents'] == [1,2,6]
    assert [i for kind,i in calls if kind=='quotes'] == [2]
    assert modes == [('idle_market_closed',0),('warming_market_open',1),
                     ('draining_market_close',3),('idle_market_closed',4),
                     ('idle_market_closed',5),('warming_market_open',6)]


@pytest.mark.asyncio
async def test_cold_offhours_start_creates_no_database(tmp_path, monkeypatch):
    marker=tmp_path/'enabled'; marker.touch()
    stop=asyncio.Event()
    original=observer.ExitEdgeLiveRecorder.heartbeat
    def heartbeat(self, **kwargs):
        original(self, **kwargs)
        stop.set()
    monkeypatch.setattr(observer.ExitEdgeLiveRecorder, 'heartbeat', heartbeat)
    await observer.run_observer(db_path=tmp_path/'db',event_db_path=tmp_path/'events',
        status_path=tmp_path/'status',enable_marker=marker,stop=stop,
        now_fn=lambda:datetime(2026,11,26,15,tzinfo=UTC))
    assert not (tmp_path/'db').exists()
    health=json.loads((tmp_path/'status').read_text())
    assert health['collection_state']=='idle_market_closed'
    assert not health['ready'] and not health['worker_alive']


def test_cold_idle_status_is_healthy_only_outside_session(tmp_path):
    from bhiksha.tools.launchd_status import _exit_edge_observer_status
    marker=tmp_path/'artifacts/playbook/runtime_flags/exit_edge_live_shadow.enabled'
    marker.parent.mkdir(parents=True); marker.touch()
    status=tmp_path/'artifacts/observations/exit_edge_live_status.json'
    status.parent.mkdir(parents=True)
    for now,expected in ((datetime(2026,11,27,18,tzinfo=UTC),'idle_market_closed'),
                         (datetime(2026,11,27,17,tzinfo=UTC),'observer_not_ready')):
        status.write_text(json.dumps({'updated_at':now.isoformat(),'role':'observer',
            'ready':False,'worker_alive':False,'collection_state':'idle_market_closed'}))
        assert _exit_edge_observer_status(tmp_path,{'loaded':True},now)['status']==expected
    status.write_text(json.dumps({'updated_at':now.isoformat(),'role':'observer',
        'ready':True,'worker_alive':False,'collection_state':'idle_market_closed'}))
    assert _exit_edge_observer_status(tmp_path,{'loaded':True},now)['status']=='observer_not_ready'

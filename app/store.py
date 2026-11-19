import os,sqlite3,json,hashlib
from datetime import datetime, timezone
from pathlib import Path
from app.errors import DomainError
from app.normalize import serial as _serial

def _dumps(value):
    return json.dumps(_serial(value),ensure_ascii=False,sort_keys=True)
def connection():
    path=Path(os.environ.get("DATA_DIR","data"));path.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(path/"runs.sqlite",timeout=15)
    db.row_factory=sqlite3.Row
    db.execute("CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY,payload TEXT NOT NULL,version INTEGER NOT NULL DEFAULT 1,review TEXT NOT NULL DEFAULT '待复核')")
    db.execute("""CREATE TABLE IF NOT EXISTS daily_settlements(
        station TEXT NOT NULL,day TEXT NOT NULL,result_id TEXT NOT NULL,input_hash TEXT NOT NULL,
        rules_version TEXT NOT NULL,payload TEXT NOT NULL,version INTEGER NOT NULL DEFAULT 1,
        review TEXT NOT NULL DEFAULT '待复核',updated_at TEXT NOT NULL,
        PRIMARY KEY(station,day))""")
    db.execute("""CREATE TABLE IF NOT EXISTS daily_settlement_versions(
        station TEXT NOT NULL,day TEXT NOT NULL,version INTEGER NOT NULL,result_id TEXT NOT NULL,
        payload TEXT NOT NULL,archived_at TEXT NOT NULL,PRIMARY KEY(station,day,version))""")
    db.execute("""CREATE TABLE IF NOT EXISTS daily_events(
        id INTEGER PRIMARY KEY AUTOINCREMENT,station TEXT NOT NULL,day TEXT NOT NULL,
        event TEXT NOT NULL,result_id TEXT,detail TEXT NOT NULL,created_at TEXT NOT NULL)""")
    return db
def save(result):
    with connection() as db:
        db.execute("INSERT OR IGNORE INTO runs(id,payload) VALUES(?,?)",(result["manifest"]["run_id"],json.dumps(result,ensure_ascii=False)))
    return fetch(result["manifest"]["run_id"])
def fetch(run_id):
    with connection() as db: row=db.execute("SELECT * FROM runs WHERE id=?",(run_id,)).fetchone()
    if row is None: raise DomainError("NOT_FOUND","批次不存在",404)
    result=json.loads(row["payload"]);result["version"]=row["version"];result["review"]=row["review"]
    return result
def history():
    with connection() as db: rows=db.execute("SELECT id,version,review FROM runs ORDER BY rowid DESC LIMIT 100").fetchall()
    return [dict(row) for row in rows]
def review(run_id,version,decision):
    if decision not in ("通过","退回"): raise DomainError("REVIEW","请选择通过或退回")
    if not isinstance(version,int): raise DomainError("STALE","版本必须为整数")
    with connection() as db:
        cur=db.execute("UPDATE runs SET review=?,version=version+1 WHERE id=? AND version=?",(decision,run_id,version))
        if cur.rowcount!=1: raise DomainError("STALE","复核版本已更新",409)
    return fetch(run_id)

def _now():
    return datetime.now(timezone.utc).isoformat()

def make_result_id(station,day,input_hash,rules_version):
    key=hashlib.sha256(f"{station}|{day}|{input_hash}|{rules_version}".encode()).hexdigest()
    return key[:20]

def record_daily_event(station,day,event,result_id,detail):
    with connection() as db:
        db.execute("INSERT INTO daily_events(station,day,event,result_id,detail,created_at) VALUES(?,?,?,?,?,?)",
                   (station,day,event,result_id or "",detail,_now()))

def upsert_daily(day_result,flows,input_hash,rules_version):
    """同站按日唯一写入。返回 (行数据, 动作)，动作取 新建/幂等/重算/锁定。

    已复核日结锁定不覆盖；相同输入重试不新增版本；重算先归档旧版再原子替换，
    任一步骤失败整体回滚，旧版日结原样保留。
    """
    station,day=day_result["station"],day_result["day"]
    result_id=make_result_id(station,day,input_hash,rules_version)
    payload=_dumps({"day":day_result,"flows":flows})
    with connection() as db:
        existing=db.execute("SELECT * FROM daily_settlements WHERE station=? AND day=?",(station,day)).fetchone()
        if existing is not None and existing["review"]!="待复核":
            return _daily_row(existing),"锁定"
        if existing is not None and existing["input_hash"]==input_hash and existing["rules_version"]==rules_version:
            db.execute("INSERT INTO daily_events(station,day,event,result_id,detail,created_at) VALUES(?,?,?,?,?,?)",
                       (station,day,"幂等重试",result_id,"相同输入与规则版本，复用既有日结",_now()))
            return _daily_row(existing),"幂等"
        try:
            if existing is not None:
                db.execute("INSERT INTO daily_settlement_versions(station,day,version,result_id,payload,archived_at) VALUES(?,?,?,?,?,?)",
                           (station,day,existing["version"],existing["result_id"],existing["payload"],_now()))
                next_version=existing["version"]+1
                db.execute("""UPDATE daily_settlements SET result_id=?,input_hash=?,rules_version=?,payload=?,
                              version=?,review='待复核',updated_at=? WHERE station=? AND day=?""",
                           (result_id,input_hash,rules_version,payload,next_version,_now(),station,day))
                action="重算"
            else:
                db.execute("""INSERT INTO daily_settlements(station,day,result_id,input_hash,rules_version,payload,version,review,updated_at)
                              VALUES(?,?,?,?,?,?,'1','待复核',?)""",
                           (station,day,result_id,input_hash,rules_version,payload,_now()))
                action="新建"
            db.execute("INSERT INTO daily_events(station,day,event,result_id,detail,created_at) VALUES(?,?,?,?,?,?)",
                       (station,day,action,result_id,f"{action}日结 V{next_version if action=='重算' else 1}",_now()))
        except Exception:
            db.rollback()
            raise
        row=db.execute("SELECT * FROM daily_settlements WHERE station=? AND day=?",(station,day)).fetchone()
        return _daily_row(row),action

def _daily_row(row):
    data=json.loads(row["payload"])
    out={"station":row["station"],"day":row["day"],"result_id":row["result_id"],
         "input_hash":row["input_hash"],"rules_version":row["rules_version"],
         "version":row["version"],"review":row["review"],"updated_at":row["updated_at"]}
    out.update(data["day"])
    out["flows"]=data["flows"]
    out["station"]=row["station"]
    out["day"]=row["day"]
    return out

def list_daily():
    with connection() as db:
        rows=db.execute("SELECT * FROM daily_settlements ORDER BY day DESC,station ASC").fetchall()
        return [_daily_row(r) for r in rows]

def fetch_daily(station,day):
    with connection() as db:
        row=db.execute("SELECT * FROM daily_settlements WHERE station=? AND day=?",(station,day)).fetchone()
        if row is None: raise DomainError("NOT_FOUND","该站点当日没有日结",404)
        return _daily_row(row)

def list_versions(station,day):
    with connection() as db:
        rows=db.execute("SELECT station,day,version,result_id,archived_at FROM daily_settlement_versions WHERE station=? AND day=? ORDER BY version DESC",(station,day)).fetchall()
        return [dict(r) for r in rows]

def fetch_version(station,day,version):
    with connection() as db:
        row=db.execute("SELECT * FROM daily_settlement_versions WHERE station=? AND day=? AND version=?",(station,day,version)).fetchone()
        if row is None: raise DomainError("NOT_FOUND","该日结版本不存在",404)
        out={"station":station,"day":day,"result_id":row["result_id"],"version":row["version"],"archived_at":row["archived_at"]}
        payload=json.loads(row["payload"])
        out.update(payload["day"]);out["flows"]=payload["flows"]
        out["station"]=station;out["day"]=day
        return out

def list_events(station=None,day=None):
    sql="SELECT * FROM daily_events";args=[]
    if station:sql+=" WHERE station=?";args.append(station)
    if day:sql+=(" AND" if station else " WHERE")+" day=?";args.append(day)
    sql+=" ORDER BY id DESC LIMIT 200"
    with connection() as db:
        return [dict(r) for r in db.execute(sql,args).fetchall()]

def review_daily(station,day,version,decision):
    if decision not in ("通过","退回"): raise DomainError("REVIEW","请选择通过或退回")
    if not isinstance(version,int): raise DomainError("STALE","版本必须为整数")
    with connection() as db:
        cur=db.execute("UPDATE daily_settlements SET review=?,version=version+1 WHERE station=? AND day=? AND version=? AND review='待复核'",
                       (decision,station,day,version))
        if cur.rowcount!=1: raise DomainError("STALE","日结版本已更新或已复核锁定",409)
        db.execute("INSERT INTO daily_events(station,day,event,result_id,detail,created_at) VALUES(?,?,?,?,?,?)",
                   (station,day,"复核"+decision,"",f"V{version} 复核{decision}",_now()))
    return fetch_daily(station,day)

import sqlite3,json,os,hashlib
from pathlib import Path
from app.errors import DomainError

def connection():
    path=Path(os.environ.get('DB_PATH','data/records.db'));path.parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(path,timeout=10);db.row_factory=sqlite3.Row
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, body TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1, decision TEXT NOT NULL DEFAULT "待复核")')
    db.execute('CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY,value TEXT NOT NULL)')
    return db

def save(run_id, body):
    """保存批次。新批次写入与“最新批次”指针在同一事务内提交。

    失败时事务回滚，旧批次（含其复核版本与结论）原样保留，供继续核验；
    同批次重试只忽略重复主键，不会覆盖已封存的旧内容，也不会重复累计。
    """
    payload=json.dumps(body,ensure_ascii=False)
    try:
        with connection() as db:
            db.execute('INSERT OR IGNORE INTO runs(id,body) VALUES (?,?)',(run_id,payload))
            db.execute("INSERT INTO meta(key,value) VALUES ('latest',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(run_id,))
            row=db.execute('SELECT * FROM runs WHERE id=?',(run_id,)).fetchone()
    except sqlite3.Error as error:
        raise DomainError('SAVE','保存失败，已保留上一版本供核验',503) from error
    if not row:raise DomainError('SAVE','保存失败，已保留上一版本供核验',503)
    result=json.loads(row['body'])
    result.update(run_id=row['id'],review_version=row['version'],decision=row['decision'])
    return result

def get(run_id):
    with connection() as db:r=db.execute('SELECT * FROM runs WHERE id=?',(run_id,)).fetchone()
    if not r:raise DomainError('NOT_FOUND','批次不存在',404)
    body=json.loads(r['body']);body.update(run_id=r['id'],review_version=r['version'],decision=r['decision']);return body

def latest():
    with connection() as db:
        row=db.execute("SELECT value FROM meta WHERE key='latest'").fetchone()
        return row['value'] if row else None

def previous_for(content):
    """返回同源输入的上一批次，供“只重算受影响时段”复用。

    只按来源内容匹配，不按参数匹配：校准补齐后参数会变（新批次），但来源
    观测相同，因此可以安全地只重算签名发生变化的时段。只回看最近批次，
    批次很多时不会把整个库读进内存。
    """
    digest=hashlib.sha256(content).hexdigest()
    with connection() as db:
        rows=db.execute('SELECT body FROM runs ORDER BY rowid DESC LIMIT 200').fetchall()
    for row in rows:
        try:
            body=json.loads(row['body'])
        except ValueError:
            continue
        if (body.get('manifest') or {}).get('sha256')==digest:
            return body
    return None

def history():
    with connection() as db:rows=db.execute("SELECT id,version,decision FROM runs ORDER BY CASE WHEN id=(SELECT value FROM meta WHERE key='latest') THEN 0 ELSE 1 END,rowid DESC LIMIT 100").fetchall()
    return [dict(r) for r in rows]

def review(run_id, version, decision):
    if decision not in ('通过','退回'):raise DomainError('DECISION','复核状态无效')
    with connection() as db:
        cur=db.execute('UPDATE runs SET version=version+1,decision=? WHERE id=? AND version=?',(decision,run_id,version))
        if cur.rowcount!=1:
            if not db.execute('SELECT 1 FROM runs WHERE id=?',(run_id,)).fetchone():raise DomainError('NOT_FOUND','批次不存在',404)
            raise DomainError('STALE','复核版本已更新，请刷新',409)
    return get(run_id)

import base64
from flask import request,render_template,jsonify
from app.errors import DomainError
from app.config import TITLE,MODULES,SHORT
from app.workflow import run,load
from app import store

def payload():
    data=request.get_json(silent=True) or {}
    if not isinstance(data,dict):raise DomainError('PAYLOAD','请求必须是对象')
    if 'csv' in data and not isinstance(data['csv'],str):raise DomainError('CSV','CSV内容必须是文本')
    sample=data.get('sample',request.args.get('sample','dirty.csv'))
    content=data.get('csv')
    return (content.encode() if isinstance(content,str) else load(sample)),data.get('parameters',{})

def api(stage):
    content,parameters=payload();previous=store.previous_for(content)
    return jsonify(run(content,parameters,stage,previous))

def page(stage,template):
    latest=store.history();state=store.get(latest[0]['id']) if latest else run(load('dirty.csv'),{})
    source=load(request.args['sample']) if 'sample' in request.args else base64.b64decode(state['source_b64']) if state.get('source_b64') else load('dirty.csv')
    parameters=state.get('manifest',{}).get('parameters',{})
    data=run(source,parameters,stage,store.previous_for(source))
    # 采信页M1在原始观测之外还要展示每段的校准依据与修正前后区间，
    # 因此额外跑一遍到估界阶段；其余页面直接用本阶段数据。
    adoption=run(source,parameters,5,store.previous_for(source)) if stage==1 else None
    return render_template(template,title=TITLE,short=SHORT,modules=MODULES,current=stage,data=data,run=state,history=latest,adoption=adoption)

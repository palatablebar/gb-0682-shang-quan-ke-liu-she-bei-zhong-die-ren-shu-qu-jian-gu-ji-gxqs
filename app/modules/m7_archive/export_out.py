import io,csv,json,hashlib

def bundle(state):
    out=io.StringIO(newline='');writer=csv.writer(out)
    writer.writerow(['记录类型','批次','来源行','业务ID','原因码','内容'])
    rid=state.get('run_id','preview')
    writer.writerow(['运行清单',rid,'','','',json.dumps(state.get('manifest',{}),ensure_ascii=False,sort_keys=True,default=str)])
    writer.writerow(['对账',rid,'','','',json.dumps(state.get('reconcile',{}),ensure_ascii=False,sort_keys=True,default=str)])
    calibration=state.get('calibration') or {}
    if calibration:
        writer.writerow(['校准裁决',rid,'','','',json.dumps({k:calibration.get(k) for k in ('maintenance','confirmed_observations','carried_observations','uncorrected_observations','calibration_quarantine','affected_buckets','conflict_calibrations')},ensure_ascii=False,sort_keys=True,default=str)])
        for row in calibration.get('records',[]):
            writer.writerow(['校准账本',rid,'',row.get('id',''),row.get('status',''),json.dumps(row,ensure_ascii=False,sort_keys=True,default=str)])
        for row in calibration.get('audit',[]):
            writer.writerow(['校准审计',rid,'',row.get('calibration_id',''),row.get('event',''),json.dumps(row,ensure_ascii=False,sort_keys=True,default=str)])
    for row in state.get('observations',[]):
        evidence=row.get('calibration') or {}
        writer.writerow(['观测采信',rid,row.get('_line',''),row.get('id',''),evidence.get('status',''),json.dumps({'bucket':row.get('bucket'),'kind':row.get('kind'),'left':row.get('left'),'right':row.get('right'),'version':row.get('version'),'unit':row.get('unit'),'before':evidence.get('before'),'after':evidence.get('after'),'delta':evidence.get('delta'),'basis':evidence.get('basis'),'calibration_ids':evidence.get('calibration_ids'),'source':evidence.get('source')},ensure_ascii=False,sort_keys=True,default=str)])
    for row in state.get('calculations',[]):writer.writerow(['计算',rid,row.get('line',''),row.get('id',''),row.get('code',''),json.dumps(row,ensure_ascii=False,sort_keys=True,default=str)])
    for row in state['quarantine']:
        content={'raw':row.get('raw',{})}
        for key in ('bucket','calibration','bucket_withheld','reused'):
            if key in row:content[key]=row[key]
        writer.writerow(['隔离',rid,row['line'],row.get('id',''),row['code'],json.dumps(content,ensure_ascii=False,sort_keys=True,default=str)])
    return '﻿'+out.getvalue()

def checksum(state):
    return hashlib.sha256(bundle(state).encode('utf-8')).hexdigest()

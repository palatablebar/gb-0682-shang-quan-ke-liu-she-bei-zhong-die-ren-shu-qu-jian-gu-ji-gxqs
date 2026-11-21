from app.modules.m7_archive.export_out import checksum

def execute(state):
    calibration=state.get('calibration') or {}
    state['archive']={'sha256':checksum(state),'evidence_count':len(state.get('calculations',[]))+len(state['quarantine']),
        'ready':state.get('reconcile',{}).get('balanced',False),
        'calibration_summary':{k:calibration.get(k) for k in ('confirmed_observations','carried_observations','uncorrected_observations','calibration_quarantine','conflict_calibrations')},
        'recompute':state.get('recompute',{})}
    return state

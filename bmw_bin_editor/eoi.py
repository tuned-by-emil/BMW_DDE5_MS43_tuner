"""Read-only EOI approximation. All angles returned positive AFTER TDC."""
import numpy as np
from .audit import interpolate

def estimate_end(rpm, soi, duration, duration_unit, soi_convention):
    rpm,soi,duration=np.broadcast_arrays(np.asarray(rpm,float),np.asarray(soi,float),np.asarray(duration,float))
    if duration_unit not in ('us','ms','deg'): raise ValueError('Choose duration units.')
    if soi_convention not in ('BTDC','ATDC'): raise ValueError('Choose the start-angle sign convention.')
    span=duration if duration_unit=='deg' else duration*rpm*(6e-6 if duration_unit=='us' else .006)
    end=(-soi if soi_convention=='BTDC' else soi)+span
    valid=np.isfinite(rpm)&np.isfinite(soi)&np.isfinite(duration)&(rpm>0)&(duration>0)&(span<720)
    return np.where(valid,end,np.nan)

def table_series(doc, definition, xs, ys):
    x=np.asarray(doc.axis_values(definition.x_axis),float);y=np.asarray(doc.axis_values(definition.y_axis),float)
    if not len(x) or not len(y) or not np.isfinite(x).all() or not np.isfinite(y).all() or np.any(np.diff(x)<=0) or np.any(np.diff(y)<=0):
        raise ValueError('Table requires finite, strictly increasing physical axes.')
    result=np.full(len(xs),np.nan)
    for i,(a,b) in enumerate(zip(xs,ys)):
        if np.isfinite(a) and np.isfinite(b) and x[0]<=a<=x[-1] and y[0]<=b<=y[-1]:result[i]=interpolate(doc,definition,float(a),float(b))
    return result

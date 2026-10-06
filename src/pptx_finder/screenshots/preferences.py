"""Screenshot-only preferences; capture geometry is bound to a display layout."""
import json, os, uuid
from pathlib import Path

DEFAULT_KEYS={'capture':'Ctrl+Alt+A','repeat':'Ctrl+Alt+R'}


class CapturePreferences:
    def __init__(self,path: Path):
        self.path=path
        self.error=''
        try:
            self.values=json.loads(path.read_text(encoding='utf-8')) if path.is_file() else {}
            if not isinstance(self.values,dict):raise ValueError('截图偏好格式无效')
        except (OSError,ValueError) as exc:
            self.values={};self.error=f'读取截图偏好失败：{exc}'

    def keys(self):
        saved=self.values.get('keys',{})
        return {key:saved.get(key,default) if isinstance(saved,dict) else default
                for key,default in DEFAULT_KEYS.items()}

    def region(self):
        region=self.values.get('region')
        try:
            if not isinstance(region,dict) or not isinstance(region['screen'],str):return None
            area,bounds,pixels=region['area'],region['bounds'],region['pixels']
            if len(area)!=4 or len(bounds)!=4 or len(pixels)!=2:return None
            if any(type(n)!=int for n in [*area,*bounds,*pixels]):return None
            x,y,w,h=area
            if min(x,y)<0 or min(w,h)<4 or min(*pixels)<=0:return None
            if x+w>bounds[2] or y+h>bounds[3]:return None
            return region
        except (KeyError,TypeError,ValueError):return None

    def update(self,**changes):
        # Different controllers may share this file. Read before merge so a
        # hotkey update never replaces a region saved by the capture window.
        latest=CapturePreferences(self.path)
        if latest.error:raise ValueError(latest.error)
        values=dict(latest.values,**changes)
        self.path.parent.mkdir(parents=True,exist_ok=True)
        temporary=self.path.with_name(self.path.name+'.'+uuid.uuid4().hex+'.tmp')
        try:
            temporary.write_text(json.dumps(values,ensure_ascii=False),encoding='utf-8')
            os.replace(temporary,self.path)
        finally:
            temporary.unlink(missing_ok=True)
        self.values=values
        self.error=''

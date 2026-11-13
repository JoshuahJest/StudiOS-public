from pathlib import Path
from urllib.parse import urljoin
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests, zipfile, json, hashlib, io
from PIL import Image

ROOT = Path(__file__).resolve().parent
RAW = ROOT / 'source'
RAW.mkdir(parents=True, exist_ok=True)
SOURCES = {
    'RHPS_Theater_Kit.zip': ('https://rockytalkiepodcast.s3.amazonaws.com/RHPS+Theater+Kit.zip', 'Fox/Fan Club promotional theater kit, mirrored by Rocky Talkie; usage rights require review'),
    'DoubleFeature20.ttf': ('https://www.rockyhorror.com/images/downloads/DoubleFeature20.ttf', 'SIL Open Font License 1.1 per official fan site'),
    'Rocky_AOE.zip': ('https://dl.dafont.com/dl/?f=rocky_aoe', 'Dafont lists 100% free; inspect bundled license before redistribution'),
}

def fetch(item):
    name, (url, rights) = item
    path = RAW / name
    if not path.exists():
        with requests.get(url, timeout=60, stream=True, headers={'User-Agent':'Mozilla/5.0'}) as response:
            response.raise_for_status()
            with path.open('wb') as f:
                for chunk in response.iter_content(1024*1024):
                    f.write(chunk)
    return {'file': name, 'url': url, 'rights_note': rights, 'bytes':path.stat().st_size, 'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}

with ThreadPoolExecutor(max_workers=3) as pool:
    records = list(pool.map(fetch, SOURCES.items()))

for record in records:
    path = RAW / record['file']
    if path.suffix.lower() != '.zip': continue
    folder = RAW / path.stem
    folder.mkdir(exist_ok=True)
    with zipfile.ZipFile(path) as z:
        for info in z.infolist():
            target = (folder / info.filename).resolve()
            if not target.is_relative_to(folder.resolve()): raise ValueError('unsafe zip member')
            if info.is_dir(): target.mkdir(parents=True, exist_ok=True); continue
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                with z.open(info) as src, target.open('wb') as dst:
                    import shutil
                    shutil.copyfileobj(src, dst)

inventory=[]
for path in sorted(RAW.rglob('*')):
    if not path.is_file() or path.suffix.lower() not in ('.jpg','.jpeg','.png','.tif','.tiff','.gif','.bmp','.ttf','.otf','.eps','.pdf'): continue
    record={'path':str(path.relative_to(ROOT)), 'bytes':path.stat().st_size}
    try:
        with Image.open(path) as im:
            record.update(width=im.width, height=im.height, mode=im.mode)
    except (OSError, ValueError): pass
    inventory.append(record)
(ROOT/'sources.json').write_text(json.dumps(records,indent=2)+'\n')
(ROOT/'inventory.json').write_text(json.dumps(inventory,indent=2)+'\n')
print('Sources:', [(x['file'],x['bytes']) for x in records]); print('Asset inventory:',len(inventory))
for x in inventory: print(x['path'], x.get('width'),x.get('height'),x.get('mode'),x['bytes'])

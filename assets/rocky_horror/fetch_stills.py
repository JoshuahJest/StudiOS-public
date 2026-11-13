from pathlib import Path
import requests, re, json, hashlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from PIL import Image
ROOT=Path(__file__).resolve().parent
URL='https://www.themoviedb.org/movie/36685-the-rocky-horror-picture-show/images/backdrops'
s=requests.get(URL,timeout=30).text
pattern=r'<li[^>]*class="card compact[^>]*>.*?</li>'
items=[]
for block in re.findall(pattern,s,re.S):
    match=re.search(r'href="(https://image\.tmdb\.org/t/p/original/[^" ]+\.jpg)"',block)
    dim=re.search(r'View Original"[^>]*>(\d+)x(\d+)</a>',block)
    if not match or not dim: continue
    width,height=map(int,dim.groups())
    if width < 1920 or height < 1080: continue
    if match.group(1) in [x['url'] for x in items]:continue
    items.append({'url':match.group(1),'width':width,'height':height,'source_page':URL,'rights_note':'TMDB community still; copyright of film/rightsholders, no reuse license conveyed'})
items=items[:30]
folder=ROOT/'source'/'tmdb_stills';folder.mkdir(exist_ok=True)
def fetch(pair):
    i,item=pair
    path=folder/f'RHPS_still_{i:02d}.jpg'
    if not path.exists():
        r=requests.get(item['url'],timeout=60);r.raise_for_status()
        if not r.headers.get('Content-Type','').startswith('image/'): raise ValueError(item['url'])
        path.write_bytes(r.content)
    with Image.open(path) as im: item['actual_dimensions']=list(im.size)
    item['file']=str(path.relative_to(ROOT));item['sha256']=hashlib.sha256(path.read_bytes()).hexdigest();item['bytes']=path.stat().st_size
    return item
with ThreadPoolExecutor(max_workers=5) as pool:
    results=sorted((future.result() for future in as_completed([pool.submit(fetch,p) for p in enumerate(items,1)])),key=lambda x:x['file'])
(ROOT/'stills_manifest.json').write_text(json.dumps(results,indent=2)+'\n')
print('Downloaded',len(results),'stills')
for r in results: print(r['file'],r['actual_dimensions'],r['bytes'])

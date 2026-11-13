from pathlib import Path
import json
from PIL import Image, ImageFilter
from rembg import remove,new_session
ROOT=Path(__file__).resolve().parent
OUT=ROOT/'transparent_png';OUT.mkdir(exist_ok=True)
manifest=json.loads((ROOT/'cutout_manifest.json').read_text())
known={x['file'] for x in manifest}
session=new_session('u2netp')
for source in sorted((ROOT/'source/tmdb_stills').glob('*.jpg')):
    name=source.stem+'_auto.png';target=OUT/name
    if str(target.relative_to(ROOT)) in known and target.exists(): continue
    image=Image.open(source).convert('RGB')
    scale=min(1,1280/max(image.size))
    small=image.resize((round(image.width*scale),round(image.height*scale)),Image.Resampling.LANCZOS)
    cut=remove(small,session=session,post_process_mask=True)
    alpha=cut.getchannel('A').resize(image.size,Image.Resampling.LANCZOS).filter(ImageFilter.GaussianBlur(.65))
    image.putalpha(alpha)
    bbox=alpha.getbbox()
    if not bbox: print('EMPTY',name,flush=True);continue
    image=image.crop(bbox)
    image.save(target,optimize=True)
    a=image.getchannel('A'); fraction=round(1-a.histogram()[255]/(image.width*image.height),3)
    row={'file':str(target.relative_to(ROOT)),'source':str(source.relative_to(ROOT)),'method':'u2netp automatic foreground mask; UNREVIEWED draft, may select wrong subject or omit limbs','width':image.width,'height':image.height,'alpha_extrema':a.getextrema(),'transparent_fraction':fraction}
    manifest.append(row)
    (ROOT/'cutout_manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
    print(name,image.size,fraction,flush=True)
print('Total cutouts',len(manifest))

from pathlib import Path
from PIL import Image,ImageDraw,ImageFont
ROOT=Path(__file__).resolve().parent
font=ImageFont.truetype('/System/Library/Fonts/Supplemental/Arial.ttf',20)
for kind,files in [('source_stills',sorted((ROOT/'source/tmdb_stills').glob('*.jpg'))),('transparent_cutouts',sorted((ROOT/'transparent_png').glob('*.png')))]:
    w,h,cols=320,218,5
    rows=(len(files)+cols-1)//cols
    sheet=Image.new('RGB',(cols*w,rows*h),(24,24,24))
    for i,path in enumerate(files):
        with Image.open(path) as im:
            im.thumbnail((w-12,h-38),Image.Resampling.LANCZOS)
            tile=Image.new('RGB',(w,h),(30,30,30))
            d=ImageDraw.Draw(tile)
            for y in range(0,h-28,16):
                for x in range(0,w,16):
                    if (x//16+y//16)%2==0:d.rectangle((x,y,x+15,y+15),fill=(65,65,65))
            if im.mode=='RGBA': tile.paste(im,((w-im.width)//2,(h-28-im.height)//2),im)
            else:tile.paste(im,((w-im.width)//2,(h-28-im.height)//2))
            d.text((8,h-25),path.stem,fill='white',font=font)
            sheet.paste(tile,((i%cols)*w,(i//cols)*h))
    path=ROOT/f'{kind}_contact_sheet.jpg';sheet.save(path,quality=88)
    print(path,len(files),sheet.size,path.stat().st_size)

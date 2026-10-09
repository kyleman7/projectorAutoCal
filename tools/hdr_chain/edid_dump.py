import sys
def mfg(b): v=(b[8]<<8)|b[9]; return "".join(chr(((v>>s)&31)+64) for s in (10,5,0))
def desc(b):
    out=[]
    for o in range(54,126,18):
        d=b[o:o+18]
        if d[0]|d[1]:
            pc=(d[0]|d[1]<<8)*10; h=d[2]|(d[4]&0xF0)<<4; v=d[5]|(d[7]&0xF0)<<4
            out.append(f"DTD {h}x{v} {pc/1000:.2f} MHz")
        elif d[3]==0xFC: out.append("name "+d[5:].decode(errors='replace').split('\n')[0].strip())
        elif d[3]==0xFD: out.append(f"range V {d[5]}-{d[6]} Hz H {d[7]}-{d[8]} kHz maxclk {d[9]*10} MHz")
    return out
def cea(x):
    print(f"  CEA rev {x[1]}: underscan {bool(x[3]&0x80)} basic-audio {bool(x[3]&0x40)} YCbCr444 {bool(x[3]&0x20)} YCbCr422 {bool(x[3]&0x10)}")
    dtd=x[2]; i=4; vics=[]
    while i<dtd:
        tag=x[i]>>5; n=x[i]&31; p=x[i+1:i+1+n]
        if tag==2:
            vics=[(v&0x7F if v<=0x80 or v>=0xC1 and False else v&0x7F if v<128 else v, v&0x80 and (v&0x7F)<=64) for v in p]
            print("  VICs:", " ".join(f"{v}{'*' if nat else ''}" for v,nat in vics))
        elif tag==3:
            oui=p[0]|p[1]<<8|p[2]<<16
            if oui==0x000C03:
                dc=p[5] if n>5 else 0
                print(f"  HDMI VSDB: deep colour {'48 ' if dc&0x40 else ''}{'36 ' if dc&0x20 else ''}{'30 ' if dc&0x10 else ''}bit, DC_Y444 {bool(dc&8)}, max TMDS {p[6]*5 if n>6 else '?'} MHz")
            elif oui==0xC45DD8:
                print(f"  HDMI Forum VSDB: max TMDS char rate {p[4]*5} MHz, SCDC {bool(p[5]&0x80)}, 4:2:0 deep colour {'36 ' if p[6]&4 else ''}{'30 ' if p[6]&2 else ''}{'48' if p[6]&1 else ''}".rstrip())
            else: print(f"  VSDB OUI {oui:06X}")
        elif tag==7:
            ext=p[0]
            if ext==5: print(f"  Colorimetry: {[nm for bit,nm in [(0x80,'BT2020RGB'),(0x40,'BT2020YCC'),(0x20,'BT2020cYCC'),(1,'xvYCC601'),(2,'xvYCC709')] if p[1]&bit]}")
            elif ext==6:
                e=p[1]; print(f"  HDR static metadata: EOTF {[nm for bit,nm in [(1,'SDR'),(2,'HDR-gamma'),(4,'PQ/ST2084'),(8,'HLG')] if e&bit]}" + (f", max lum code {p[3]}" if n>3 else ""))
            elif ext==14: print("  4:2:0-only VICs:", list(p[1:]))
            elif ext==15: print("  4:2:0 capability map:", p[1:].hex())
            elif ext==0: print("  Video capability:", p[1:].hex())
            else: print(f"  ext tag {ext}")
        elif tag==1: print(f"  Audio block ({n//3} formats)")
        elif tag==4: print("  Speaker allocation")
        i+=1+n
for f in sys.argv[1:]:
    b=open(f,"rb").read()
    print(f"== {f}: {mfg(b)} product {b[10]|b[11]<<8:04X}, week {b[16]} {1990+b[17]}, ext blocks {b[126]}, checksums ok {sum(b[:128])%256==0 and (len(b)<256 or sum(b[128:256])%256==0)}")
    for d in desc(b): print("  ",d)
    if len(b)>=256 and b[128]==2: cea(b[128:256])

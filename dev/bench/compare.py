"""Numpy/Pillow-only display-referred image comparison and HTML report."""
from __future__ import annotations
import argparse, base64, io, json, os, statistics
import numpy as np
from PIL import Image

BG = 0.4613  # 18 % linear grey in sRGB; both renders have a transparent background composited onto it

def _rgba(p):
    return np.asarray(Image.open(p).convert("RGBA"),dtype=np.float32)/255.0
def _image(p):
    """Straight-alpha PNG over the shared grey background, so backgrounds never differ."""
    x=_rgba(p); a=x[...,3:4]
    return x[...,:3]*a+BG*(1-a)
def _mask_of(p):
    return _rgba(p)[...,3]>0.5
def _blur(x):
    k=np.array([1,4,6,4,1],dtype=np.float32)/16
    y=np.pad(x,((0,0),(2,2)),mode="edge")
    y=sum(k[i]*y[:,i:i+x.shape[1]] for i in range(5))
    y=np.pad(y,((2,2),(0,0)),mode="edge")
    return sum(k[i]*y[i:i+x.shape[0],:] for i in range(5))
def _ssim(a,b):
    la=.2126*a[...,0]+.7152*a[...,1]+.0722*a[...,2]; lb=.2126*b[...,0]+.7152*b[...,1]+.0722*b[...,2]
    ma,mb=_blur(la),_blur(lb); va=_blur(la*la)-ma*ma; vb=_blur(lb*lb)-mb*mb; cov=_blur(la*lb)-ma*mb
    return float(np.mean(((2*ma*mb+0.01**2)*(2*cov+0.03**2))/((ma*ma+mb*mb+0.01**2)*(va+vb+0.03**2))))
def _lab(x):
    v=np.where(x<=.04045,x/12.92,((x+.055)/1.055)**2.4)
    m=np.array([[.4124564,.3575761,.1804375],[.2126729,.7151522,.072175],[.0193339,.119192,.9503041]])
    q=np.einsum("...c,dc->...d",v,m)/np.array([.95047,1,1.08883])  # D65 white
    f=np.where(q>0.008856,q**(1/3),7.787*q+16/116)
    return np.stack((116*f[...,1]-16,500*(f[...,0]-f[...,1]),200*(f[...,1]-f[...,2])),axis=-1)
def score(a,b,ma,mb):
    m=ma|mb
    de=np.sqrt(np.sum((_lab(a)-_lab(b))**2,axis=2))
    return {"ssim":_ssim(a,b),"delta_e":float(np.mean(de[m])) if np.any(m) else 0.0,
            "mask_iou":float(np.sum(ma&mb)/max(1,np.sum(ma|mb))),
            "mean_rgb_a":np.mean(a[m],axis=0).round(6).tolist() if np.any(m) else [0,0,0],
            "mean_rgb_b":np.mean(b[m],axis=0).round(6).tolist() if np.any(m) else [0,0,0]}
def _data_uri(path):
    with open(path,"rb") as f: return "data:image/png;base64,"+base64.b64encode(f.read()).decode("ascii")
def _heatmap(a,b,path):
    d=np.clip(np.abs(a-b)*4,0,1)
    Image.fromarray((d*255).astype(np.uint8),"RGB").save(path)
def main():
    ap=argparse.ArgumentParser(); ap.add_argument("--root",required=True); ap.add_argument("--limit",type=int,default=0); ap.add_argument("--self",action="store_true"); args=ap.parse_args()
    rows=[]; assets=json.load(open(os.path.join(os.path.dirname(__file__),"assets.json"),encoding="utf-8"))
    assets=assets[:args.limit or None]
    for e in assets:
        d=os.path.join(args.root,e["base_id"]); ref=os.path.join(d,"ref.png"); other=ref if args.self else os.path.join(d,"c4d_redshift.png")
        r={"base_id":e["base_id"],"name":e["name"]}
        try: r.update(score(_image(ref),_image(other),_mask_of(ref),_mask_of(other)))
        except Exception as exc: r["error"]=str(exc)
        rows.append(r)
    good=[r for r in rows if "ssim" in r]; good.sort(key=lambda r:r["ssim"])
    result={"results":good,"errors":[r for r in rows if "error" in r],"summary":{"median_ssim":statistics.median([r["ssim"] for r in good]) if good else None,"median_delta_e":statistics.median([r["delta_e"] for r in good]) if good else None,"mask_iou_lt_0_9":sum(r["mask_iou"]<.9 for r in good)}}
    json.dump(result,open(os.path.join(args.root,"results.json"),"w"),indent=2)
    html=["<html><body><h1>Kitbridge fidelity benchmark</h1><table><tr><th>Asset</th><th>SSIM</th><th>Delta E</th><th>IoU</th><th>Reference / comparison / |diff|</th></tr>"]
    for r in good:
        d=os.path.join(args.root,r["base_id"]); a_path=os.path.join(d,"ref.png"); b_path=a_path if args.self else os.path.join(d,"c4d_redshift.png")
        heat=os.path.join(d,"diff.png"); _heatmap(_image(a_path),_image(b_path),heat)
        html.append("<tr><td>%s</td><td>%.6f</td><td>%.4f</td><td>%.6f</td><td><img width=160 src='%s'><img width=160 src='%s'><img width=160 src='%s'></td></tr>"%(r["name"],r["ssim"],r["delta_e"],r["mask_iou"],_data_uri(a_path),_data_uri(b_path),_data_uri(heat)))
    html.append("</table></body></html>"); open(os.path.join(args.root,"report.html"),"w",encoding="utf-8").write("\n".join(html))
    print(json.dumps(result["summary"]))
if __name__=="__main__": main()

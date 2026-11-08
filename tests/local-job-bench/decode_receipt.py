"""Decode the bounded synthetic CSV receipt shown on the test form, not arbitrary clipboard/files."""
from __future__ import annotations
import argparse, hashlib, struct, subprocess, tempfile
from pathlib import Path


def screenshot_pixels(path):
    # sips handles PNG/JPEG. The checked macOS BMP layout is little-endian BGR.
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "image.bmp"
        subprocess.run(["sips", "-s", "format", "bmp", str(path), "--out", str(target)], check=True, stdout=subprocess.DEVNULL)
        image = target.read_bytes()
    if image[:2] != b"BM":
        raise ValueError("Invalid BMP")
    offset = int.from_bytes(image[10:14], "little")
    width, height = struct.unpack("<ii", image[18:26])
    planes, depth, compression = struct.unpack("<HHI", image[26:34])
    if not (width > 0 and height and planes == 1 and depth in (24,32) and compression in (0,3)):
        raise ValueError(f"Unsupported raster: {width},{height},{depth},{compression}")
    stride = (width * depth + 31) // 32 * 4
    def px(x,y):
        pos = offset + ((height - 1 - y) if height > 0 else y)*stride + x*(depth//8)
        return tuple(image[pos:pos+3][::-1])
    return width, abs(height), px


def decode(path):
    w,h,px=screenshot_pixels(path)
    # Only scan the fixture's visual receipt area at nominal unscaled/125%/150% DPI.
    for y in range(125, min(h, 360)):
        for x in range(610, min(w, 1040)):
            r,g,b=px(x,y)
            if r<190 or b<190 or g>65: continue
            # Find cyan tag on the same row. Its distance is (160*3 + 6) px.
            for cx in range(x+450,min(w,x+760)):
                r,g,b=px(cx,y)
                if r>65 or g<190 or b<190: continue
                scale=(cx-x)/486.0
                def read(length):
                    result=bytearray()
                    for n in range(length*8):
                        col,row=n%160,n//160
                        c=px(round(x+(7+col*3)*scale),round(y+(7+row*3)*scale))
                        if n%8==0:result.append(0)
                        if sum(c)<384:result[-1] |= 128>>(n%8)
                    return bytes(result)
                head=read(39)
                if head[:5]!=b"LPQA1": continue
                n=int.from_bytes(head[5:7],"big")
                if n>8192: raise ValueError("Out-of-scope receipt length")
                blob=read(39+n)[39:]
                if hashlib.sha256(blob).digest()!=head[7:39]: raise ValueError("Receipt checksum mismatch")
                return blob
    raise ValueError("No verified fixture receipt in screenshot")


if __name__=="__main__":
    p=argparse.ArgumentParser();p.add_argument("screenshot",type=Path);p.add_argument("output",type=Path)
    a=p.parse_args();data=decode(a.screenshot);a.output.write_bytes(data);print(f"{len(data)} bytes SHA256 {hashlib.sha256(data).hexdigest()}")

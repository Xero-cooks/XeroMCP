from pathlib import Path
p = Path(r'C:\Users\User\Downloads\testers\MCPbridges\MCPbridges\.mcp_inspections\latest.jpg')
print('exists', p.exists(), 'size', p.stat().st_size if p.exists() else 0)
try:
    from PIL import Image
    img = Image.open(p)
    print('image', img.size, img.mode)
except Exception as e:
    print('PIL_ERR', type(e).__name__, e)
try:
    import pytesseract
    from PIL import Image
    print('OCR_START')
    print(pytesseract.image_to_string(Image.open(p)))
    print('OCR_END')
except Exception as e:
    print('TESS_ERR', type(e).__name__, e)

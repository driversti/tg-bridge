"""Terminal QR rendering for the login script."""

import io

import qrcode


def render_qr(data: str) -> str:
    qr = qrcode.QRCode(border=2)
    qr.add_data(data)
    qr.make(fit=True)
    out = io.StringIO()
    # invert=True: dark modules drawn as spaces reads correctly on dark terminal themes.
    qr.print_ascii(out=out, invert=True)
    return out.getvalue()

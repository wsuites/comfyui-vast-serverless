#!/usr/bin/env python3
"""
Normaliza una tira de sprites RGBA para que sirvan como ciclo de animacion.

ControlNet impone la pose pero no el TAMANO: el modelo dibuja al personaje mas
grande o mas pequeno segun le parece, y en la animacion eso se ve como un
"hipo". Aqui se corrige en post, que es determinista y no depende de que el
modelo obedezca.

No se normaliza a una altura unica: eso agrandaria las poses recogidas, que
legitimamente ocupan menos. Se normaliza contra la altura del ESQUELETO que
genero cada frame, asi se respeta la anatomia y solo se quita la deriva.

    python normalizar.py --in sprites_pose2 --out sprites_norm
"""
from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image

HERE = Path(__file__).parent


def caja_alfa(im: Image.Image):
    a = im.getchannel("A")
    return a.getbbox()          # (izq, arr, der, aba) de lo no transparente


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="entrada", default="sprites_pose2")
    ap.add_argument("--out", dest="salida", default="sprites_norm")
    ap.add_argument("--poses", default="poses")
    ap.add_argument("--lienzo", type=int, default=1024)
    ap.add_argument("--alto-base", type=int, default=780,
                    help="altura en px del frame mas alto del ciclo")
    ap.add_argument("--suelo", type=float, default=0.94)
    args = ap.parse_args()

    ent = HERE / args.entrada
    sal = HERE / args.salida
    sal.mkdir(parents=True, exist_ok=True)
    L = args.lienzo
    suelo_y = int(L * args.suelo)

    rutas = sorted(ent.glob("sprite_*.png"))
    if not rutas:
        raise SystemExit(f"no hay sprites en {ent}")

    # altura y elevacion de cada esqueleto: es la referencia de proporcion
    ref_alto, ref_aire = [], []
    for i in range(len(rutas)):
        p = HERE / args.poses / f"pose_{i+1:02d}.png"
        if not p.is_file():
            ref_alto.append(1.0); ref_aire.append(0); continue
        sk = Image.open(p).convert("L")
        c = sk.point(lambda v: 255 if v > 20 else 0).getbbox()
        ref_alto.append(c[3] - c[1])
        ref_aire.append(int(L * 0.90) - c[3])      # cuanto despega del suelo
    tope = max(ref_alto)

    salidas = []
    for i, r in enumerate(rutas):
        im = Image.open(r).convert("RGBA")
        c = caja_alfa(im)
        if not c:
            print(f"{r.name}: vacio, se salta"); continue
        recorte = im.crop(c)
        objetivo = args.alto_base * (ref_alto[i] / tope)
        escala = objetivo / recorte.size[1]
        nuevo = (max(1, int(recorte.size[0] * escala)),
                 max(1, int(recorte.size[1] * escala)))
        recorte = recorte.resize(nuevo, Image.LANCZOS)

        lienzo = Image.new("RGBA", (L, L), (0, 0, 0, 0))
        x = (L - nuevo[0]) // 2
        aire = int(ref_aire[i] * escala)
        y = suelo_y - aire - nuevo[1]
        lienzo.paste(recorte, (x, y), recorte)
        destino = sal / r.name
        lienzo.save(destino)
        salidas.append(lienzo)
        print(f"{r.name}: caja {c[2]-c[0]}x{c[3]-c[1]} -> {nuevo[0]}x{nuevo[1]}"
              f"  aire={aire}px")

    if not salidas:
        return
    # hoja sobre tablero
    T, cols = 256, 4
    filas = (len(salidas) + cols - 1) // cols
    fondo = Image.new("RGB", (T*cols, T*filas), (235, 235, 235))
    for yy in range(0, T*filas, 16):
        for xx in range(0, T*cols, 16):
            if (xx//16 + yy//16) % 2:
                fondo.paste((202, 202, 202), (xx, yy, xx+16, yy+16))
    for k, im in enumerate(salidas):
        t = im.resize((T, T), Image.LANCZOS)
        fondo.paste(t, ((k % cols)*T, (k//cols)*T), t)
    fondo.save(HERE / "sprites_norm_hoja.png")
    print("sprites_norm_hoja.png")

    salidas[0].save(HERE / "sprites_norm.gif", save_all=True,
                    append_images=salidas[1:], duration=110, loop=0,
                    disposal=2, transparency=0)
    print("sprites_norm.gif")

    alturas = [caja_alfa(im)[3] - caja_alfa(im)[1] for im in salidas]
    pies = [caja_alfa(im)[3] for im in salidas]
    print("alturas:", alturas)
    print("linea de pies:", pies)


if __name__ == "__main__":
    main()

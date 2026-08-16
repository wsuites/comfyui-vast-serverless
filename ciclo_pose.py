#!/usr/bin/env python3
"""
Genera esqueletos OpenPose (COCO-18) de un ciclo de carrera, vista lateral
mirando a la derecha.

No dependemos del detector: dibujamos los esqueletos nosotros, que era la pieza
que fallaba (OpenposePreprocessor no lee bien el estilo anime).

    python ciclo_pose.py --frames 8 --out poses
"""
from __future__ import annotations

import argparse, math
from pathlib import Path

from PIL import Image, ImageDraw

# COCO-18: 0 nariz 1 cuello 2 hombroD 3 codoD 4 munecaD 5 hombroI 6 codoI
# 7 munecaI 8 caderaD 9 rodillaD 10 tobilloD 11 caderaI 12 rodillaI 13 tobilloI
# 14 ojoD 15 ojoI 16 orejaD 17 orejaI
LIMBS = [(1,2),(1,5),(2,3),(3,4),(5,6),(6,7),(1,8),(8,9),(9,10),
         (1,11),(11,12),(12,13),(1,0),(0,14),(14,16),(0,15),(15,17)]
COLORS = [(255,0,0),(255,85,0),(255,170,0),(255,255,0),(170,255,0),(85,255,0),
          (0,255,0),(0,255,85),(0,255,170),(0,255,255),(0,170,255),(0,85,255),
          (0,0,255),(85,0,255),(170,0,255),(255,0,255),(255,0,170),(255,0,85)]

# 4 poses clave del ciclo. Angulos en grados desde la vertical hacia abajo,
# positivo = hacia delante (derecha).
#   (muslo, flexion_rodilla)  y  (brazo_superior, flexion_codo)
# 'aire' = cuanto se separa del suelo el pie mas bajo. El anclaje al suelo es
# lo que evita que el personaje cambie de tamano entre frames: las longitudes
# de los miembros ya son constantes, lo que variaba era la altura del conjunto.
CLAVES = [
    # contacto: pierna delantera estirada tocando el suelo
    dict(aire=0,  muslo_a=28,  rod_a=12,  muslo_b=-30, rod_b=58,
         hom_a=-38, cod_a=55,  hom_b=38,  cod_b=70),
    # amortiguacion: peso sobre la pierna de apoyo, cuerpo bajo
    dict(aire=0,  muslo_a=6,   rod_a=48,  muslo_b=-44, rod_b=95,
         hom_a=-18, cod_a=70,  hom_b=20,  cod_b=80),
    # paso: la pierna trasera adelanta con la rodilla alta
    dict(aire=14, muslo_a=-16, rod_a=18,  muslo_b=12,  rod_b=105,
         hom_a=12,  cod_a=75,  hom_b=-14, cod_b=60),
    # impulso: fase de vuelo, ambos pies despegados
    dict(aire=58, muslo_a=-36, rod_a=32,  muslo_b=42,  rod_b=76,
         hom_a=34,  cod_a=60,  hom_b=-36, cod_b=55),
]

SUELO = 0.90        # linea de suelo, en fraccion de la altura del lienzo


def rad(g): return math.radians(g)


def punto(origen, ang_grados, longitud):
    """Desde 'origen', un segmento de 'longitud' con angulo desde la vertical."""
    a = rad(ang_grados)
    return (origen[0] + longitud * math.sin(a), origen[1] + longitud * math.cos(a))


def esqueleto(clave, w, h, espejo=False):
    """Devuelve los 18 keypoints. espejo=True intercambia las dos piernas/brazos."""
    k = dict(clave)
    if espejo:
        k = dict(k, muslo_a=clave["muslo_b"], rod_a=clave["rod_b"],
                 muslo_b=clave["muslo_a"], rod_b=clave["rod_a"],
                 hom_a=clave["hom_b"], cod_a=clave["cod_b"],
                 hom_b=clave["hom_a"], cod_b=clave["cod_a"])

    cx = w * 0.46
    cadera_y = h * 0.52
    L_MUSLO, L_TIBIA = h * 0.165, h * 0.165
    L_TORSO = h * 0.235
    L_BRAZO, L_ANTE = h * 0.125, h * 0.115
    INCL = 14                                    # inclinacion del torso al correr

    cadera = (cx, cadera_y)
    cuello = punto(cadera, 180 + INCL, L_TORSO)  # hacia arriba, inclinado
    nariz = punto(cuello, 180 + INCL - 8, h * 0.085)

    p = [None] * 18
    p[1] = cuello
    p[0] = nariz
    p[8] = p[11] = cadera
    p[2] = p[5] = (cuello[0], cuello[1] + h * 0.012)

    # piernas
    for lado, (mus, rodf, i_rod, i_tob) in enumerate(
            [(k["muslo_a"], k["rod_a"], 9, 10), (k["muslo_b"], k["rod_b"], 12, 13)]):
        rodilla = punto(cadera, mus, L_MUSLO)
        p[i_rod] = rodilla
        p[i_tob] = punto(rodilla, mus - rodf, L_TIBIA)

    # brazos (contrarios a las piernas)
    for lado, (hom, codf, i_cod, i_mun) in enumerate(
            [(k["hom_a"], k["cod_a"], 3, 4), (k["hom_b"], k["cod_b"], 6, 7)]):
        codo = punto(p[2], hom, L_BRAZO)
        p[i_cod] = codo
        p[i_mun] = punto(codo, hom + codf, L_ANTE)

    # cara de perfil mirando a la derecha
    d = h * 0.018
    p[14] = (nariz[0] - d * 0.6, nariz[1] - d)      # ojo derecho
    p[15] = (nariz[0] - d * 1.2, nariz[1] - d)      # ojo izquierdo (oculto)
    p[16] = (cuello[0] + d * 0.4, cuello[1] - h * 0.055)
    p[17] = (cuello[0] - d * 0.4, cuello[1] - h * 0.055)

    # anclar al suelo: el pie mas bajo cae en la linea de suelo, salvo el
    # levantamiento intencionado de 'aire'. Asi el personaje no cambia de
    # tamano ni flota entre frames.
    pie_mas_bajo = max(p[10][1], p[13][1])
    dy = (h * SUELO - k["aire"]) - pie_mas_bajo
    p = [(q[0], q[1] + dy) for q in p]
    return p


def dibujar(p, w, h):
    img = Image.new("RGB", (w, h), (0, 0, 0))
    dr = ImageDraw.Draw(img)
    grosor = max(4, w // 128)
    for i, (a, b) in enumerate(LIMBS):
        if p[a] and p[b]:
            dr.line([p[a], p[b]], fill=COLORS[i % len(COLORS)], width=grosor)
    r = max(3, w // 200)
    for i, q in enumerate(p):
        if q:
            dr.ellipse([q[0]-r, q[1]-r, q[0]+r, q[1]+r], fill=COLORS[i % len(COLORS)])
    return img


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--frames", type=int, default=8)
    ap.add_argument("--size", type=int, default=1024)
    ap.add_argument("--out", default="poses")
    a = ap.parse_args()

    dst = Path(__file__).parent / a.out
    dst.mkdir(parents=True, exist_ok=True)
    n, s = a.frames, a.size
    for i in range(n):
        # primera mitad del ciclo con una pierna, segunda con la otra
        fase = i * len(CLAVES) * 2 // n
        clave = CLAVES[fase % len(CLAVES)]
        espejo = (fase // len(CLAVES)) % 2 == 1
        img = dibujar(esqueleto(clave, s, s, espejo), s, s)
        ruta = dst / f"pose_{i+1:02d}.png"
        img.save(ruta)
        print(f"{ruta.name}  clave={fase % len(CLAVES)} espejo={espejo}")

    # tira de contactos para revisar el ciclo de un vistazo
    ims = [Image.open(dst / f"pose_{i+1:02d}.png") for i in range(n)]
    T = 200
    tira = Image.new("RGB", (T*n, T), (0, 0, 0))
    for i, im in enumerate(ims):
        tira.paste(im.resize((T, T), Image.LANCZOS), (i*T, 0))
    tira.save(Path(__file__).parent / "ciclo_pose.png")
    print("ciclo_pose.png")


if __name__ == "__main__":
    main()

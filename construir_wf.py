#!/usr/bin/env python3
"""
Genera variantes del workflow de escena a partir de wf_escena_mask.json.

    python construir_wf.py

Produce, para cada combinacion:
    wf_v_<pose>_<detalle>.json     pose = none|openpose|dwpose
                                   detalle = sd|hd|hd2|hd3
Donde 'hd' anade una segunda pasada de inpaint recortando la zona del
personaje y ampliandola, para recuperar el detalle que se pierde cuando la
figura ocupa poca parte del lienzo.

    sd    sin segunda pasada
    hd    la version original: recorte a 1024 fijo, prompt de escena, denoise 0.35
    hd2   corrige el aspecto, amplia a 1536 y usa un prompt propio del recorte
    hd3   hd2 + FaceDetailer sobre el recorte ya ampliado
"""
import json
from pathlib import Path

HERE = Path(__file__).parent
BASE = json.loads((HERE / "wf_escena_mask.json").read_text(encoding="utf-8"))

LIENZO = 1024
MARGEN = 32          # margen alrededor del personaje al recortar
DETALLE_RES = 1024   # a que resolucion se re-difunde el recorte ('hd')
DETALLE_RES2 = 1536  # lo mismo para 'hd2'/'hd3'
DETALLE_DENOISE = 0.45

# --- Resolucion del personaje sobre el escenario -----------------------------
# El personaje se genera en su propio lienzo, se recorta con BiRefNet y se
# escala a una caja dentro del escenario. Ahi se pierde resolucion por dos
# sitios distintos, y cada palanca ataca uno:
#
#   vertical  el personaje se genera en 1024x1024 CUADRADO, pero una figura de
#             cuerpo entero solo ocupa una franja vertical: el resto son
#             pixeles de fondo que BiRefNet borra despues. En 832x1216 (ratio
#             nativo de SDXL) la figura llena mucho mas el encuadre.
#   lienzo    subir todo a 1536. SDXL esta entrenado a 1024 y por encima tiende
#             a duplicar elementos, asi que es el ultimo recurso.
#   upscale   UltimateSDUpscale sobre el compuesto final.
PERSONAJE_VERTICAL = (832, 1216)   # ratio nativo SDXL para figura de pie
LIENZO_GRANDE = 1536
BBOX_PADDING = 8     # px de aire alrededor de la silueta al recortar (--bbox)
UPSCALE_MODELO = "4x_NMKD-Siax_200k.pth"

# El prompt de la segunda pasada no debe describir el escenario: el recorte ya
# esta cerrado sobre el personaje y pedir "bedroom, window light" hace que el
# modelo gaste capacidad repintando fondo en vez de piel, pelo y manos.
DETALLE_POS = ("masterpiece, best quality, solo, 1girl, long red hair, "
               "(blue eyes:1.3), (detailed face:1.2), detailed eyes, "
               "detailed hands, detailed skin, detailed fabric folds, "
               "sharp focus, high detail")
DETALLE_NEG = ("worst quality, blurry, lowres, bad hands, bad anatomy, "
               "extra fingers, watermark, text, oversharpened, jpeg artifacts")

# En un bbox de mano, "(detailed face:1.2)" es ruido: el prompt de la pasada de
# manos tiene que hablar solo de manos, y el negativo carga contra los fallos
# tipicos (dedos de mas, fusionados) porque re-difundir un bbox pequeno los
# invita.
MANOS_POS = ("masterpiece, best quality, (detailed hands:1.3), five fingers, "
             "well-drawn hands, correct hand anatomy, sharp focus")
MANOS_NEG = ("worst quality, blurry, lowres, bad hands, mutated hands, "
             "extra fingers, fused fingers, extra digits, missing fingers, "
             "deformed hands, watermark, text")
MANOS_DENOISE = 0.30    # ruta yolo: solo detalle, sin margen para reinventar
MESH_DENOISE = 0.65     # ruta MeshGraphormer: el depth sujeta la geometria

# El default del nodo es 0.6 y con anime NO detecta: devuelve un depth negro y
# la pasada no hace nada (sin error, en silencio). Medido sobre la imagen de
# prueba: a 0.6 detecta 0 manos, a 0.3 detecta 1. Sobre una foto real detecta
# a 0.6 sin problema, o sea que es el estilo lo que le cuesta, no el montaje.
MESH_DETECT_THR = 0.3


def escalado(cw: int, ch: int, res: int) -> tuple[int, int]:
    """Dimensiones del recorte ampliado, manteniendo el aspecto.

    El lado largo va a `res` y el corto se redondea a multiplo de 8 (lo que
    pide el VAE). La version original escalaba a res x res fijo, asi que un
    recorte 704x676 se deformaba un 4% de ancho, se re-difundia deformado y se
    devolvia estirado: parte de lo que ganaba en detalle lo perdia ahi.
    """
    factor = res / max(cw, ch)
    return (max(8, round(cw * factor / 8) * 8),
            max(8, round(ch * factor / 8) * 8))


def aplicar_caja(wf: dict, tam: int = 640, x: int = 200, y: int = 380,
                 lienzo: int = LIENZO, vertical: bool = False) -> dict:
    """Unica funcion que coloca la caja del personaje sobre el escenario.

    Toca los nodos de escala (26/28) y de pegado (30/51) y devuelve la
    geometria que sale, para que quien tenga que recortar despues (la 2a
    pasada) no la recalcule por su cuenta y se desincronice.

    Las palancas interactuan: 'vertical' cambia el ASPECTO de la caja y
    'lienzo' cambia su ESCALA. Antes cada una escribia 26/28 por su lado y la
    ultima en correr borraba a la anterior; ahora se combinan aqui.
    """
    f = lienzo / LIENZO
    tam = max(8, round(tam * f / 8) * 8)
    x, y = int(x * f), int(y * f)
    if vertical:
        pw, ph = PERSONAJE_VERTICAL
        wf["22"]["inputs"]["width"] = pw
        wf["22"]["inputs"]["height"] = ph
        # la caja conserva el aspecto: alto = tam, ancho proporcional. Si se
        # sigue forzando tam x tam cuadrado la figura se aplasta y se pierde
        # justo lo que se gana.
        cw, ch = max(8, round(tam * pw / ph / 8) * 8), tam
    else:
        cw = ch = tam
    for n in ("26", "28"):
        # con --bbox estos dos dejan de ser ImageScale y pasan a ser
        # ResizeAndPadImage, que llama a lo mismo target_width/target_height
        if wf[n]["class_type"] == "ResizeAndPadImage":
            wf[n]["inputs"]["target_width"] = cw
            wf[n]["inputs"]["target_height"] = ch
        else:
            wf[n]["inputs"]["width"] = cw
            wf[n]["inputs"]["height"] = ch
    for n in ("30", "51"):
        if n in wf:
            wf[n]["inputs"]["x"] = x
            wf[n]["inputs"]["y"] = y
    return {"x": x, "y": y, "cw": cw, "ch": ch, "lienzo": lienzo}


def con_vertical(wf: dict, tam: int = 640) -> dict:
    """El personaje se genera en vertical en vez de en un cuadrado."""
    aplicar_caja(wf, tam=tam, x=wf["30"]["inputs"]["x"],
                 y=wf["30"]["inputs"]["y"], vertical=True)
    return wf


def con_bbox(wf: dict) -> dict:
    """Recorta el personaje a su bounding box antes de escalarlo a la caja.

    BiRefNet devuelve el recorte sobre el lienzo entero, con alfa transparente
    alrededor. Ese padding viaja hasta la caja de destino y se lleva buena parte
    de los pixeles utiles. Recortando a la silueta, la figura aprovecha la caja
    completa.

    Necesita un nodo que calcule el bbox EN EJECUCION: el recorte depende de la
    imagen generada, no se puede precalcular aqui.

    Esquema ya verificado contra el worker vivo (python sondear_nodos.py):

        AILab_CropObject   (custom_nodes.ComfyUI-RMBG)
            opt: image:IMAGE, mask:MASK, padding:INT
            out: IMAGE, MASK

    Es el unico candidato cableable tal cual, y su pack ya se instala con
    FEAT_RMBG. Va entre BiRefNet (25) y el escalado de la caja (26/28), y hay
    que pasarle tambien la mascara para que 27/29 sigan cuadrando.

    Descartados: CropByBBoxes e ImageCropV2 exigen un tipo BOUNDING_BOX que solo
    produce un detector (rtdetr/sdpose) o el editor manual del canvas;
    AILab_ImageCrop recorta a coordenadas fijas, no al objeto.

    Recortar a la silueta obliga ademas a cambiar el escalado. La caja (26/28)
    tiene un aspecto fijo y el bbox no: si se sigue estirando con ImageScale, el
    personaje sale deformado y se pierde justo lo que se gana. Por eso 26/28
    pasan a ResizeAndPadImage (comfy_extras.nodes_images), que mete la figura
    dentro de la caja MANTENIENDO el aspecto y rellena lo que sobra de negro.

        ResizeAndPadImage
            req: image:IMAGE, target_width:INT, target_height:INT,
                 padding_color:white|black, interpolation:...|lanczos
            out: IMAGE

    El relleno negro es inocuo: la mascara (28) se rellena igual, y negro = 0 =
    "no pegues nada aqui", asi que el compuesto no ve el borde.
    """
    # el 32 esta libre y cae dentro del bloque del personaje (26-31). OJO: el
    # 55 NO lo esta, lo ocupa un MaskToImage de la 2a pasada.
    if "32" in wf:
        raise SystemExit("con_bbox: el nodo 32 ya existe, busca otro id libre")
    wf["32"] = {"class_type": "AILab_CropObject",
                "inputs": {"image": ["25", 0], "mask": ["25", 1],
                           "padding": BBOX_PADDING},
                "_meta": {"title": "recorte a la silueta del personaje"}}
    cw = wf["26"]["inputs"].get("width", 640)
    ch = wf["26"]["inputs"].get("height", 640)
    for n, fuente in (("26", ["32", 0]), ("28", ["27", 0])):
        wf[n] = {"class_type": "ResizeAndPadImage",
                 "inputs": {"image": fuente, "target_width": cw,
                            "target_height": ch, "padding_color": "black",
                            "interpolation": "lanczos"},
                 "_meta": {"title": wf[n].get("_meta", {}).get("title", "")}}
    wf["27"]["inputs"]["mask"] = ["32", 1]
    return wf


def con_lienzo(wf: dict, lado: int, tam: int = 640, x: int = 200, y: int = 380,
               vertical: bool = False) -> dict:
    """Sube el lienzo de escenario y composicion. Escala posiciones y mascara."""
    wf["12"]["inputs"]["width"] = lado
    wf["12"]["inputs"]["height"] = lado
    wf["50"]["inputs"]["width"] = lado
    wf["50"]["inputs"]["height"] = lado
    aplicar_caja(wf, tam=tam, x=x, y=y, lienzo=lado, vertical=vertical)
    return wf


def con_upscale(wf: dict, origen: list) -> dict:
    """UltimateSDUpscale sobre el compuesto final (2x)."""
    wf["130"] = {"class_type": "UpscaleModelLoader",
                 "inputs": {"model_name": UPSCALE_MODELO}}
    wf["131"] = {"class_type": "UltimateSDUpscale",
                 "inputs": {"image": origen, "model": ["2", 0],
                            "positive": ["40", 0], "negative": ["41", 0],
                            "vae": ["1", 2], "upscale_model": ["130", 0],
                            "upscale_by": 2.0, "seed": 888888, "steps": 18,
                            "cfg": 5.0, "sampler_name": "dpmpp_2m",
                            "scheduler": "karras", "denoise": 0.2,
                            "mode_type": "Linear", "tile_width": 1024,
                            "tile_height": 1024, "mask_blur": 8,
                            "tile_padding": 32, "seam_fix_mode": "None",
                            "seam_fix_denoise": 1.0, "seam_fix_width": 64,
                            "seam_fix_mask_blur": 8, "seam_fix_padding": 16,
                            "force_uniform_tiles": True,
                            "tiled_decode": False},
                 "_meta": {"title": "UltimateSDUpscale 2x del compuesto"}}
    wf["132"] = {"class_type": "SaveImage",
                 "inputs": {"filename_prefix": "h_upscale", "images": ["131", 0]},
                 "_meta": {"title": "SALIDA: compuesto ampliado 2x"}}
    return wf


def con_pose(wf: dict, detector: str) -> dict:
    """Anade guia de pose. detector: 'openpose' o 'dwpose'."""
    clase = "OpenposePreprocessor" if detector == "openpose" else "DWPreprocessor"
    inputs = {"image": ["25", 0],          # el PERSONAJE RECORTADO, no el collage:
              "detect_hand": "enable",     # sobre fondo limpio el detector acierta mas
              "detect_body": "enable",
              "detect_face": "enable",
              "resolution": 1024}
    wf["70"] = {"class_type": clase, "inputs": inputs,
                "_meta": {"title": f"esqueleto ({detector})"}}
    # el esqueleto sale del personaje aislado (1024) y hay que colocarlo donde
    # esta el personaje en el collage: se escala y se compone sobre negro
    wf["75"] = {"class_type": "ImageScale",
                "inputs": {"image": ["70", 0], "upscale_method": "nearest-exact",
                           "width": 640, "height": 640, "crop": "disabled"},
                "_meta": {"title": "escalar esqueleto como el personaje"}}
    wf["76"] = {"class_type": "EmptyImage",
                "inputs": {"width": LIENZO, "height": LIENZO, "batch_size": 1, "color": 0},
                "_meta": {"title": "lienzo negro para el esqueleto"}}
    wf["77"] = {"class_type": "ImageCompositeMasked",
                "inputs": {"destination": ["76", 0], "source": ["75", 0],
                           "x": 200, "y": 380, "resize_source": False},
                "_meta": {"title": "esqueleto en su sitio"}}
    wf["71"] = {"class_type": "SaveImage",
                "inputs": {"filename_prefix": "e_pose", "images": ["77", 0]},
                "_meta": {"title": "SALIDA: esqueleto usado"}}
    wf["72"] = {"class_type": "ControlNetLoader",
                "inputs": {"control_net_name": "controlnet-union-sdxl-1.0.safetensors"}}
    wf["73"] = {"class_type": "SetUnionControlNetType",
                "inputs": {"control_net": ["72", 0], "type": "openpose"}}
    wf["74"] = {"class_type": "ControlNetApplyAdvanced",
                "inputs": {"positive": ["40", 0], "negative": ["41", 0],
                           "control_net": ["73", 0], "image": ["77", 0],
                           "strength": 0.85, "start_percent": 0.0,
                           "end_percent": 0.8, "vae": ["1", 2]},
                "_meta": {"title": "aplicar pose"}}
    wf["43"]["inputs"]["positive"] = ["74", 0]
    wf["43"]["inputs"]["negative"] = ["74", 1]
    return wf


def con_detalle(wf: dict) -> dict:
    """Segunda pasada: recorta al personaje, amplia a 1024 y re-difunde."""
    x, y, tam = 200, 380, 640
    cx = max(0, x - MARGEN)
    cy = max(0, y - MARGEN)
    cw = min(LIENZO - cx, tam + 2 * MARGEN)
    ch = min(LIENZO - cy, tam + 2 * MARGEN)

    wf["80"] = {"class_type": "ImageCrop",
                "inputs": {"image": ["44", 0], "width": cw, "height": ch, "x": cx, "y": cy},
                "_meta": {"title": f"recorte {cw}x{ch} del personaje"}}
    wf["81"] = {"class_type": "CropMask",
                "inputs": {"mask": ["53", 0], "x": cx, "y": cy, "width": cw, "height": ch},
                "_meta": {"title": "misma mascara, recortada"}}
    wf["82"] = {"class_type": "ImageScale",
                "inputs": {"image": ["80", 0], "upscale_method": "lanczos",
                           "width": DETALLE_RES, "height": DETALLE_RES, "crop": "disabled"},
                "_meta": {"title": f"ampliar recorte a {DETALLE_RES}"}}
    wf["83"] = {"class_type": "MaskToImage", "inputs": {"mask": ["81", 0]}}
    wf["84"] = {"class_type": "ImageScale",
                "inputs": {"image": ["83", 0], "upscale_method": "bilinear",
                           "width": DETALLE_RES, "height": DETALLE_RES, "crop": "disabled"}}
    wf["85"] = {"class_type": "ImageToMask", "inputs": {"image": ["84", 0], "channel": "red"}}
    wf["86"] = {"class_type": "VAEEncode", "inputs": {"pixels": ["82", 0], "vae": ["1", 2]}}
    wf["87"] = {"class_type": "SetLatentNoiseMask",
                "inputs": {"samples": ["86", 0], "mask": ["85", 0]}}
    wf["88"] = {"class_type": "KSampler",
                "inputs": {"model": ["2", 0], "seed": 444444, "steps": 22, "cfg": 5.0,
                           "sampler_name": "dpmpp_2m", "scheduler": "karras",
                           "denoise": 0.35,
                           "positive": ["40", 0], "negative": ["41", 0],
                           "latent_image": ["87", 0]},
                "_meta": {"title": "re-difundir el recorte a alta resolucion"}}
    wf["89"] = {"class_type": "VAEDecode", "inputs": {"samples": ["88", 0], "vae": ["1", 2]}}
    wf["90"] = {"class_type": "ImageScale",
                "inputs": {"image": ["89", 0], "upscale_method": "lanczos",
                           "width": cw, "height": ch, "crop": "disabled"},
                "_meta": {"title": "devolver el recorte a su tamano"}}
    wf["91"] = {"class_type": "ImageCompositeMasked",
                "inputs": {"destination": ["44", 0], "source": ["90", 0],
                           "mask": ["81", 0], "x": cx, "y": cy, "resize_source": False},
                "_meta": {"title": "pegar el recorte mejorado"}}
    wf["92"] = {"class_type": "SaveImage",
                "inputs": {"filename_prefix": "f_detalle", "images": ["91", 0]},
                "_meta": {"title": "SALIDA: con pasada de detalle"}}
    return wf


def _manos_yolo(wf: dict, entrada: list) -> list:
    """Pasada de manos con detector yolo + FaceDetailer.

    FaceDetailer no es especifico de caras: recorta lo que le marque el
    bbox_detector. Con 'bbox/hand_yolov8s.pt' hace exactamente lo mismo con las
    manos. Devuelve la nueva salida de imagen.
    """
    wf["100"] = {"class_type": "UltralyticsDetectorProvider",
                 "inputs": {"model_name": "bbox/hand_yolov8s.pt"}}
    wf["101"] = {"class_type": "FaceDetailer",
                 "inputs": {"guide_size": 512, "guide_size_for": True, "max_size": 1024,
                            "seed": 666666, "steps": 20, "cfg": 4.0,
                            "sampler_name": "dpmpp_2m", "scheduler": "karras",
                            "denoise": MANOS_DENOISE, "feather": 5, "noise_mask": True,
                            "force_inpaint": True, "bbox_threshold": 0.4,
                            "bbox_dilation": 10, "bbox_crop_factor": 3,
                            "sam_detection_hint": "center-1", "sam_dilation": -395,
                            "sam_threshold": 0.93, "sam_bbox_expansion": 0,
                            "sam_mask_hint_threshold": 0.7,
                            "sam_mask_hint_use_negative": "False", "drop_size": 10,
                            "wildcard": "", "cycle": 1, "inpaint_model": False,
                            "noise_mask_feather": 20, "tiled_encode": False,
                            "tiled_decode": False,
                            "image": entrada, "model": ["2", 0], "clip": ["60", 0],
                            "vae": ["1", 2], "positive": ["97", 0],
                            "negative": ["98", 0], "bbox_detector": ["100", 0]},
                 "_meta": {"title": "detalle de manos (yolo)"}}
    return ["101", 0]


def _manos_mesh(wf: dict, entrada: list, dw: int, dh: int) -> list:
    """Pasada de manos con MeshGraphormer + ControlNet depth (HandRefiner).

    El nodo ajusta una malla 3D a la mano y devuelve DOS cosas: el mapa de
    profundidad de esa malla y la mascara de la zona. El depth va por el
    ControlNet union en modo depth, la mascara delimita la re-difusion. Es una
    guia de geometria, no un corrector: la pasada de inpaint hace falta igual.
    """
    wf["110"] = {"class_type": "MeshGraphormer-DepthMapPreprocessor",
                 "inputs": {"image": entrada, "mask_bbox_padding": 30,
                            "resolution": max(dw, dh), "mask_type": "based_on_depth",
                            "mask_expand": 5, "rand_seed": 88,
                            "detect_thr": MESH_DETECT_THR,
                            "presence_thr": MESH_DETECT_THR},
                 "_meta": {"title": "malla 3D de la mano -> depth + mascara"}}
    # el nodo redimensiona internamente; devolvemos depth y mascara al tamano
    # exacto del recorte para que no haya desalineo al re-difundir
    wf["111"] = {"class_type": "ImageScale",
                 "inputs": {"image": ["110", 0], "upscale_method": "bilinear",
                            "width": dw, "height": dh, "crop": "disabled"},
                 "_meta": {"title": "depth al tamano del recorte"}}
    wf["112"] = {"class_type": "MaskToImage", "inputs": {"mask": ["110", 1]}}
    wf["113"] = {"class_type": "ImageScale",
                 "inputs": {"image": ["112", 0], "upscale_method": "bilinear",
                            "width": dw, "height": dh, "crop": "disabled"}}
    wf["114"] = {"class_type": "ImageToMask", "inputs": {"image": ["113", 0], "channel": "red"}}
    wf["115"] = {"class_type": "SaveImage",
                 "inputs": {"filename_prefix": "g_depth", "images": ["111", 0]},
                 "_meta": {"title": "SALIDA: depth de la mano (para diagnostico)"}}

    wf["116"] = {"class_type": "ControlNetLoader",
                 "inputs": {"control_net_name": "controlnet-union-sdxl-1.0.safetensors"}}
    wf["117"] = {"class_type": "SetUnionControlNetType",
                 "inputs": {"control_net": ["116", 0], "type": "depth"}}
    wf["118"] = {"class_type": "ControlNetApplyAdvanced",
                 "inputs": {"positive": ["97", 0], "negative": ["98", 0],
                            "control_net": ["117", 0], "image": ["111", 0],
                            "strength": 1.0, "start_percent": 0.0,
                            "end_percent": 1.0, "vae": ["1", 2]},
                 "_meta": {"title": "aplicar depth de la mano"}}
    wf["119"] = {"class_type": "VAEEncode", "inputs": {"pixels": entrada, "vae": ["1", 2]}}
    wf["120"] = {"class_type": "SetLatentNoiseMask",
                 "inputs": {"samples": ["119", 0], "mask": ["114", 0]}}
    wf["121"] = {"class_type": "KSampler",
                 "inputs": {"model": ["2", 0], "seed": 777777, "steps": 20, "cfg": 5.0,
                            "sampler_name": "dpmpp_2m", "scheduler": "karras",
                            "denoise": MESH_DENOISE,
                            "positive": ["118", 0], "negative": ["118", 1],
                            "latent_image": ["120", 0]},
                 "_meta": {"title": "re-difundir la mano guiada por el depth"}}
    wf["122"] = {"class_type": "VAEDecode", "inputs": {"samples": ["121", 0], "vae": ["1", 2]}}
    return ["122", 0]


def con_detalle2(wf: dict, cara: bool, manos: str | None = None) -> dict:
    """Segunda pasada mejorada: aspecto correcto, mas resolucion, prompt propio.

    Con `cara`, mete ademas un FaceDetailer sobre el recorte ya ampliado, que es
    donde sale rentable: en el lienzo de 1024 la cara mide ~90 px y el detector
    tiene poco con lo que trabajar; a 1536 sobre el recorte pasa de 300 px.
    """
    x, y, tam = 200, 380, 640
    cx = max(0, x - MARGEN)
    cy = max(0, y - MARGEN)
    cw = min(LIENZO - cx, tam + 2 * MARGEN)
    ch = min(LIENZO - cy, tam + 2 * MARGEN)
    dw, dh = escalado(cw, ch, DETALLE_RES2)

    wf["80"] = {"class_type": "ImageCrop",
                "inputs": {"image": ["44", 0], "width": cw, "height": ch, "x": cx, "y": cy},
                "_meta": {"title": f"recorte {cw}x{ch} del personaje"}}
    wf["81"] = {"class_type": "CropMask",
                "inputs": {"mask": ["53", 0], "x": cx, "y": cy, "width": cw, "height": ch},
                "_meta": {"title": "misma mascara, recortada"}}
    wf["82"] = {"class_type": "ImageScale",
                "inputs": {"image": ["80", 0], "upscale_method": "lanczos",
                           "width": dw, "height": dh, "crop": "disabled"},
                "_meta": {"title": f"ampliar recorte a {dw}x{dh} (aspecto intacto)"}}
    wf["83"] = {"class_type": "MaskToImage", "inputs": {"mask": ["81", 0]}}
    wf["84"] = {"class_type": "ImageScale",
                "inputs": {"image": ["83", 0], "upscale_method": "bilinear",
                           "width": dw, "height": dh, "crop": "disabled"}}
    wf["85"] = {"class_type": "ImageToMask", "inputs": {"image": ["84", 0], "channel": "red"}}
    wf["86"] = {"class_type": "VAEEncode", "inputs": {"pixels": ["82", 0], "vae": ["1", 2]}}
    wf["87"] = {"class_type": "SetLatentNoiseMask",
                "inputs": {"samples": ["86", 0], "mask": ["85", 0]}}
    wf["93"] = {"class_type": "CLIPTextEncode",
                "inputs": {"clip": ["60", 0], "text": DETALLE_POS},
                "_meta": {"title": "positivo del recorte (sin escenario)"}}
    wf["94"] = {"class_type": "CLIPTextEncode",
                "inputs": {"clip": ["60", 0], "text": DETALLE_NEG},
                "_meta": {"title": "negativo del recorte"}}
    wf["88"] = {"class_type": "KSampler",
                "inputs": {"model": ["2", 0], "seed": 444444, "steps": 24, "cfg": 5.0,
                           "sampler_name": "dpmpp_2m", "scheduler": "karras",
                           "denoise": DETALLE_DENOISE,
                           "positive": ["93", 0], "negative": ["94", 0],
                           "latent_image": ["87", 0]},
                "_meta": {"title": "re-difundir el recorte a alta resolucion"}}
    wf["89"] = {"class_type": "VAEDecode", "inputs": {"samples": ["88", 0], "vae": ["1", 2]}}

    origen = ["89", 0]
    if cara:
        wf["96"] = {"class_type": "UltralyticsDetectorProvider",
                    "inputs": {"model_name": "bbox/face_yolov8m.pt"}}
        wf["95"] = {"class_type": "FaceDetailer",
                    "inputs": {"guide_size": 512, "guide_size_for": True, "max_size": 1024,
                               "seed": 555555, "steps": 20, "cfg": 4.0,
                               "sampler_name": "dpmpp_2m", "scheduler": "karras",
                               "denoise": 0.4, "feather": 5, "noise_mask": True,
                               "force_inpaint": True, "bbox_threshold": 0.4,
                               "bbox_dilation": 10, "bbox_crop_factor": 3,
                               "sam_detection_hint": "center-1", "sam_dilation": -395,
                               "sam_threshold": 0.93, "sam_bbox_expansion": 0,
                               "sam_mask_hint_threshold": 0.7,
                               "sam_mask_hint_use_negative": "False", "drop_size": 10,
                               "wildcard": "", "cycle": 1, "inpaint_model": False,
                               "noise_mask_feather": 20, "tiled_encode": False,
                               "tiled_decode": False,
                               "image": ["89", 0], "model": ["2", 0], "clip": ["60", 0],
                               "vae": ["1", 2], "positive": ["93", 0],
                               "negative": ["94", 0], "bbox_detector": ["96", 0]},
                    "_meta": {"title": "FaceDetailer sobre el recorte ampliado"}}
        origen = ["95", 0]

    if manos:
        wf["97"] = {"class_type": "CLIPTextEncode",
                    "inputs": {"clip": ["60", 0], "text": MANOS_POS},
                    "_meta": {"title": "positivo de manos"}}
        wf["98"] = {"class_type": "CLIPTextEncode",
                    "inputs": {"clip": ["60", 0], "text": MANOS_NEG},
                    "_meta": {"title": "negativo de manos"}}
        # el orden importa: primero la malla arregla la geometria, luego el
        # detailer anade detalle sobre una mano ya bien construida
        if manos in ("mesh", "ambas"):
            origen = _manos_mesh(wf, origen, dw, dh)
        if manos in ("yolo", "ambas"):
            origen = _manos_yolo(wf, origen)

    wf["90"] = {"class_type": "ImageScale",
                "inputs": {"image": origen, "upscale_method": "lanczos",
                           "width": cw, "height": ch, "crop": "disabled"},
                "_meta": {"title": "devolver el recorte a su tamano"}}
    wf["91"] = {"class_type": "ImageCompositeMasked",
                "inputs": {"destination": ["44", 0], "source": ["90", 0],
                           "mask": ["81", 0], "x": cx, "y": cy, "resize_source": False},
                "_meta": {"title": "pegar el recorte mejorado"}}
    wf["92"] = {"class_type": "SaveImage",
                "inputs": {"filename_prefix": "f_detalle", "images": ["91", 0]},
                "_meta": {"title": "SALIDA: con pasada de detalle"}}
    return wf


def sin_mascara(wf: dict) -> dict:
    """Quita la mascara: el img2img re-difunde el lienzo entero.

    Documentado como MALA idea (deforma el escenario en cuanto subes el
    denoise), pero es un interruptor legitimo para reproducir la comparacion.
    Los nodos hay que BORRARLOS, no solo desconectarlos: ComfyUI ejecuta todo
    lo que siga colgando de una salida.
    """
    wf["43"]["inputs"]["latent_image"] = ["42", 0]      # VAEEncode crudo
    for n in ("50", "51", "52", "53", "54", "55", "56"):
        wf.pop(n, None)
    if "81" in wf:      # la 2a pasada recortaba esa mascara: sin ella, no puede
        raise SystemExit("--sin-mascara no es compatible con la 2a pasada "
                         "(el recorte necesita la mascara del nodo 53)")
    return wf


def construir(pose: str = "dwpose", detalle: str = "hd2", cara: bool = True,
              manos: str | None = None, mascara: bool = True,
              vertical: bool = False, lienzo: int = LIENZO,
              bbox: bool = False, upscale: bool = False) -> dict:
    """Arma el workflow encendiendo y apagando cada funcionalidad.

    pose     none | openpose | dwpose   guia de pose por ControlNet
    detalle  no | hd | hd2              2a pasada sobre el recorte del personaje
    cara     bool                       FaceDetailer dentro de la 2a pasada
    manos    None | yolo | mesh | ambas pasada de manos dentro de la 2a pasada
    mascara  bool                       SetLatentNoiseMask en la fusion

    Resolucion del personaje (ver comentario de PERSONAJE_VERTICAL):
    vertical bool    generar el personaje en 832x1216 en vez de 1024 cuadrado
    lienzo   int     lado del escenario/composicion (1024 por defecto)
    bbox     bool    recortar el personaje a su bounding box antes de escalar
    upscale  bool    UltimateSDUpscale 2x sobre el compuesto final
    """
    if pose not in ("none", "openpose", "dwpose"):
        raise SystemExit(f"pose desconocida: {pose}")
    if detalle not in ("no", "hd", "hd2"):
        raise SystemExit(f"detalle desconocido: {detalle}")
    if manos not in (None, "yolo", "mesh", "ambas"):
        raise SystemExit(f"manos desconocido: {manos}")
    if detalle == "no" and (cara or manos):
        raise SystemExit("--cara y --manos viven DENTRO de la 2a pasada: "
                         "necesitan --detalle hd2")
    if detalle == "hd" and (cara or manos):
        raise SystemExit("--cara y --manos solo existen en --detalle hd2 "
                         "('hd' se conserva solo como baseline de la comparacion)")

    wf = json.loads(json.dumps(BASE))               # copia profunda
    # lienzo y vertical se resuelven de una vez (una escribe la escala y la
    # otra el aspecto de la MISMA caja: aplicadas por separado se pisaban)
    if lienzo != LIENZO or vertical:
        wf = con_lienzo(wf, lienzo, vertical=vertical)
    if bbox:
        wf = con_bbox(wf)
    if pose != "none":
        wf = con_pose(wf, pose)
    if detalle == "hd":
        wf = con_detalle(wf)
    elif detalle == "hd2":
        wf = con_detalle2(wf, cara=cara, manos=manos)
    if not mascara:
        wf = sin_mascara(wf)
    if upscale:
        # cuelga de la ultima salida que exista: 2a pasada > fusion
        wf = con_upscale(wf, ["91", 0] if "91" in wf else ["44", 0])
    return wf


# Nombres cortos usados por el A/B y por los runners. Cada uno es una
# combinacion concreta de los interruptores de arriba.
VARIANTES = {
    "sd":    dict(detalle="no",  cara=False, manos=None),
    "hd":    dict(detalle="hd",  cara=False, manos=None),
    "hd2":   dict(detalle="hd2", cara=False, manos=None),
    "hd3":   dict(detalle="hd2", cara=True,  manos=None),
    "hd3y":  dict(detalle="hd2", cara=True,  manos="yolo"),
    "hd3m":  dict(detalle="hd2", cara=True,  manos="mesh"),
    "hd3ym": dict(detalle="hd2", cara=True,  manos="ambas"),
}


def anadir_flags(p) -> None:
    """Anade a un ArgumentParser los interruptores del workflow.

    Lo usan escena.py, correr.py y ab_detalle.py para que los mismos flags
    signifiquen lo mismo en todas partes.
    """
    g = p.add_argument_group("interruptores del workflow")
    g.add_argument("--pose", default="dwpose", choices=("none", "openpose", "dwpose"),
                   help="guia de pose por ControlNet (default: dwpose)")
    g.add_argument("--detalle", default="hd2", choices=("no", "hd", "hd2"),
                   help="2a pasada sobre el recorte del personaje (default: hd2)")
    g.add_argument("--cara", action="store_true", help="FaceDetailer en la 2a pasada")
    g.add_argument("--manos", choices=("yolo", "mesh", "ambas"),
                   help="pasada de manos en la 2a pasada")
    g.add_argument("--sin-mascara", action="store_true",
                   help="quita SetLatentNoiseMask (deforma el escenario)")
    g.add_argument("--variante", choices=tuple(VARIANTES),
                   help="atajo con nombre; ignora --detalle/--cara/--manos")
    g.add_argument("--workflow", help="usar este .json en vez de armarlo con los flags")
    r = p.add_argument_group("resolucion del personaje")
    r.add_argument("--vertical", action="store_true",
                   help=f"generar el personaje en {PERSONAJE_VERTICAL[0]}x{PERSONAJE_VERTICAL[1]}")
    r.add_argument("--lienzo", type=int, default=LIENZO,
                   help=f"lado del escenario (default {LIENZO}; {LIENZO_GRANDE} duplica elementos)")
    r.add_argument("--bbox", action="store_true",
                   help="recortar el personaje a su bounding box")
    r.add_argument("--upscale", action="store_true",
                   help="UltimateSDUpscale 2x del compuesto final")


def desde_flags(a) -> dict:
    """Devuelve el workflow: del fichero --workflow, o armado con los flags."""
    if getattr(a, "workflow", None):
        return json.loads((HERE / a.workflow).read_text(encoding="utf-8"))
    opts = dict(VARIANTES[a.variante]) if getattr(a, "variante", None) else \
        dict(detalle=a.detalle, cara=a.cara, manos=a.manos)
    # las palancas de resolucion son ortogonales a la variante: se aplican
    # siempre, tambien cuando se usa --variante como atajo
    for k in ("vertical", "lienzo", "bbox", "upscale"):
        opts[k] = getattr(a, k, LIENZO if k == "lienzo" else False)
    return construir(pose=a.pose, mascara=not a.sin_mascara, **opts)


def main() -> None:
    import argparse
    p = argparse.ArgumentParser(
        description="Arma el workflow de escena encendiendo/apagando cada parte.",
        epilog="Ejemplos:\n"
               "  python construir_wf.py --matriz\n"
               "  python construir_wf.py --pose dwpose --detalle hd2 --cara "
               "--manos yolo -o mi_wf.json\n"
               "  python construir_wf.py --variante hd3y -o mi_wf.json",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--pose", default="dwpose", choices=("none", "openpose", "dwpose"))
    p.add_argument("--detalle", default="hd2", choices=("no", "hd", "hd2"))
    p.add_argument("--cara", action="store_true", help="FaceDetailer en la 2a pasada")
    p.add_argument("--manos", choices=("yolo", "mesh", "ambas"))
    p.add_argument("--sin-mascara", action="store_true",
                   help="quita SetLatentNoiseMask (deforma el escenario)")
    p.add_argument("--variante", choices=tuple(VARIANTES),
                   help="atajo: aplica una combinacion con nombre")
    p.add_argument("-o", "--salida", help="fichero donde escribir el workflow")
    p.add_argument("--matriz", action="store_true",
                   help="regenera todos los wf_v_<pose>_<variante>.json")
    a = p.parse_args()

    if a.matriz:
        for pose in ("none", "openpose", "dwpose"):
            for nombre, opts in VARIANTES.items():
                wf = construir(pose=pose, **opts)
                f = HERE / f"wf_v_{pose}_{nombre}.json"
                f.write_text(json.dumps(wf, indent=2, ensure_ascii=False), encoding="utf-8")
                print(f"{f.name}: {len(wf)} nodos")
        return

    opts = dict(VARIANTES[a.variante]) if a.variante else \
        dict(detalle=a.detalle, cara=a.cara, manos=a.manos)
    wf = construir(pose=a.pose, mascara=not a.sin_mascara, **opts)

    texto = json.dumps(wf, indent=2, ensure_ascii=False)
    if a.salida:
        (HERE / a.salida).write_text(texto, encoding="utf-8")
        activo = [f"pose={a.pose}", f"detalle={opts['detalle']}"]
        if opts.get("cara"):
            activo.append("cara")
        if opts.get("manos"):
            activo.append(f"manos={opts['manos']}")
        if a.sin_mascara:
            activo.append("SIN mascara")
        print(f"{a.salida}: {len(wf)} nodos  [{', '.join(activo)}]")
    else:
        print(texto)


if __name__ == "__main__":
    main()

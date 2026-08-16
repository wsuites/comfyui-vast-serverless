import argparse, asyncio, json, uuid
import escena, ab_resolucion as ab, construir_wf
from vastai import Serverless
from config import ENDPOINT_NAME as ENDPOINT

p = argparse.ArgumentParser(); construir_wf.anadir_flags(p)
for f in ('--escena','--personaje','--fusion','--prompt-detalle'): p.add_argument(f, type=str)
p.add_argument('--seed-escena', type=int, default=111111); p.add_argument('--seed-personaje', type=int, default=222222)
p.add_argument('--seed-fusion', type=int, default=333333); p.add_argument('--denoise', type=float, default=0.4)
p.add_argument('--tam', type=int, default=640); p.add_argument('--x', type=int, default=200); p.add_argument('--y', type=int, default=380)
base = p.parse_args([])

async def main():
    a = ab.args_de('u', base)
    wf = escena.construir(a)
    print('nodos', len(wf), 'SaveImage:', sorted(k for k,v in wf.items() if v['class_type']=='SaveImage'), flush=True)
    cli = Serverless(api_key=escena.api_key())
    try:
        ep = await cli.get_endpoint(name=ENDPOINT)
        res = await ep.request("/generate/sync", {"input": {"request_id": str(uuid.uuid4()), "workflow_json": wf}}, cost=100, timeout=900)
    finally:
        await cli.close()
    open('res_u.json','w',encoding='utf-8').write(json.dumps(res, indent=2, ensure_ascii=False))
    print('salidas parseadas:', [o.get('filename') for o in escena.salidas(res)], flush=True)
asyncio.run(main())

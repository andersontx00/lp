"""No-network smoke tests for product identity and listing quality.

Run as part of Render startup when RADAR_MATCH_SELFTEST=1.  Tests must not
consume provider queries or write to the database.
"""
from __future__ import annotations
import json
from collections import defaultdict
from pathlib import Path

POSITIVE=[
 ("Core 4","Tênis Olympikus Corre 4 Eleva Pro 2.0 original"),
 ("Olympikus Corre 4","Tênis Olympikus Corre4 original"),
 ("Olympikus Corre 4","Olympikus Corre 4 Eleva Pro 2.0"),
 ("Olympikus Corre 3","Olympikus Corre 3 Masculino"),
 ("Olympikus Corre 5","Olympikus Corre 5 Original"),
 ("Olympikus Corre Grafeno 2","Tênis Olympikus Corre Grafeno 2"),
 ("Olympikus Corre Supra","Tênis Olympikus Corre Supra"),
 ("Logitech G305 LIGHTSPEED","Mouse Logitech G305 LIGHTSPEED"),
 ("Logitech G502 X","Mouse Logitech G502 X com fio"),
 ("Logitech G502 X LIGHTSPEED","Mouse Logitech G502 X LIGHTSPEED sem fio"),
 ("Logitech G502 X PLUS","Mouse Logitech G502 X PLUS RGB"),
 ("Samsung Galaxy S24 FE 256GB","Smartphone Samsung Galaxy S24 FE 256GB"),
 ("Apple iPhone 15 Pro","Apple iPhone 15 Pro 128GB"),
 ("NVIDIA GeForce RTX 4060 Ti","ASUS GeForce RTX 4060 Ti 8GB"),
 ("Brastemp BLF10BR 220V","Lava-louças Brastemp BLF10BR 220V"),
 ("Honda Civic LXS manual 2015","Honda Civic LXS Manual 2015"),
]
NEGATIVE=[
 ("Core 4","Tênis Olympikus Eleva 4"),
 ("Olympikus Corre 4","Olympikus Eleva 4"),
 ("Olympikus Corre 4","Tênis Olympikus Eleva Pro Corre 4"),
 ("Olympikus Corre 4","Olympikus Corre 5"),
 ("Olympikus Corre 3","Olympikus Corre 4"),
 ("Olympikus Corre 4","Olympikus Corre 4 ou Corre 5"),
 ("Olympikus Corre 4","Olympikus Corre Grafeno 2"),
 ("Olympikus Corre 4","Tênis Olympikus Corre 4 Grafeno"),
 ("Olympikus Corre Grafeno 2","Olympikus Corre 2"),
 ("Olympikus Corre Supra","Tênis Olympikus Corre 4 Supra"),
 ("Olympikus Corre 4","Tênis Adidas Corre 4"),
 ("Olympikus Corre 4","Capa para tênis Olympikus Corre 4"),
 ("Samsung Galaxy S24 FE 256GB","Capinha Samsung Galaxy S24 FE 256GB"),
 ("Samsung Galaxy S24 FE 256GB","Smartphone Samsung Galaxy S24 FE 128GB"),
 ("Samsung Galaxy S24 FE 256GB","Samsung Galaxy S25 FE 256GB"),
 ("Apple iPhone 15 128GB","Apple iPhone 15 256GB"),
 ("Apple iPhone 15","Apple iPhone 15 Pro"),
 ("Apple iPhone 15 Pro","Apple iPhone 15 Pro Max"),
 ("NVIDIA GeForce RTX 4060","GeForce RTX 4060 Ti 8GB"),
 ("Logitech G502 X","Logitech G502 X PLUS"),
 ("Logitech G502 X","Logitech G502 X LIGHTSPEED"),
 ("Logitech G502 X LIGHTSPEED","Logitech G502 X wired"),
 ("Logitech G305","Mouse Logitech G309"),
 ("Honda Civic LXS manual 2015","Honda Civic LXR manual 2015"),
 ("Honda Civic LXS manual 2015","Honda Civic LXS CVT 2015"),
 ("Brastemp BLF10BR 220V","Brastemp BLF10BR 127V"),
]

def run(matcher,extractor,canonicalizer,catalog_file=None):
    failures=[]
    for wanted,title in POSITIVE:
        score=matcher(wanted,title)
        if score<.72:failures.append(('valid_rejected',wanted,title,score))
    for wanted,title in NEGATIVE:
        score=matcher(wanted,title)
        if score>=.72:failures.append(('wrong_accepted',wanted,title,score))
    if canonicalizer('Core 4')!='Olympikus Corre 4':
        failures.append(('canonicalization','Core 4'))
    # The actual provider parser must exclude the cheaper wrong model.
    rows={'shopping_results':[
       {'title':'Tênis Olympikus Eleva 4','extracted_price':59.0,'link':'https://example.org/eleva','source':'A'},
       {'title':'Tênis Olympikus Corre 4 Eleva Pro 2.0','extracted_price':449.0,'link':'https://example.org/corre','source':'B'}
    ]}
    offers=extractor(rows,'Olympikus Corre 4')
    if len(offers)!=1 or offers[0]['price']!=449.0:
        failures.append(('extractor_accepted_wrong_model',len(offers)))
    collisions=[]
    if catalog_file and Path(catalog_file).exists():
        data=json.loads(Path(catalog_file).read_text(encoding='utf-8'))
        groups=defaultdict(list)
        for product in data:
            if product.get('kind')=='model':
                groups[(product.get('category'),product.get('subcategory'))].append(product['name'])
        for names in groups.values():
            for wanted in names:
                for title in names:
                    if wanted==title:continue
                    if matcher(wanted,title)>=.72:
                        collisions.append((wanted,title))
    if failures:
        raise AssertionError('Product identity tests failed: '+repr(failures[:12]))
    return {'passed_cases':len(POSITIVE)+len(NEGATIVE)+2,
            'cross_catalog_matches':len(collisions),
            'cross_catalog_examples':collisions[:10]}

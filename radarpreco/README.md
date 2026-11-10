# RadarPreço 9 — testes de plataforma

Código para o site público `https://radarpreco-anderson-web.onrender.com`.

## Fase 1 — Confiabilidade
- Compatibilidade com Postgres (`DATABASE_URL`), além de SQLite local para desenvolvimento.
- Sessão de conta `HttpOnly` + `SameSite=Lax`; autenticação e CSRF nos endpoints privados.
- Senhas com PBKDF2 SHA-256 e salt único; não há recuperação de senha por e-mail nem confirmação de endereço ainda.
- Comparação de variantes: modelos, capacidades, versões, cores e voltagem explícita.
- Data da leitura em cada oferta; ofertas mais velhas que 24 h não são tratadas como atuais.
- Cota diária e mensal do lado do servidor (proteção adicional, NÃO substitui um gateway de segurança para produção).

## Fase 2 — Inteligência de preços
- Histórico por loja com dados reais, calculado por datas diferentes (sem interpolar cotações inexistentes).
- Indicador de oportunidade quando há pelo menos três dias anteriores de dados.
- Alertas de queda percentual por loja e produto; sincronizados com a conta ou salvos por navegador convidado.
- Scanner de produtos acompanhados enquanto o serviço está ativo; **não funciona com garantia 24h no Render Free**.

## Fase 3 — Interface
- Cards de ofertas recentes alimentados apenas pelo banco de preços (últimas 24 h).
- Imagens quando a fonte fornece; fallback seguro se indisponíveis.
- Comparação de preços e custo total **somente se o valor do frete estiver informado**; valores de Pix/cupons ausentes não são inventados.
- Navegação inferior mobile, botões acessíveis, compartilhamento por URL, instalação PWA com manifest e ícones.

## Fase 4 — Contas e produto
- Cadastro/login de teste com e-mail e senha; favoritos sincronizados em conta.
- Acompanhamentos de navegador podem ser migrados para a conta.
- Notificações locais enquanto a aba está aberta, se a pessoa autorizar; **push de verdade com o app fechado não está ativo**.
- Plano gratuito registrado em contas e plano Pro **ilustrativo**; nenhum pagamento foi habilitado.
- Login Google, verificação de e-mail, recuperação de senha, gateway e notificações push são pendências externas para uso comercial.

## Dados e custos
- O banco **Render Postgres Free expira em 30 dias**. O banco de testes criado em 10/11/2026 expira em 10/12/2026. Isso não é armazenamento permanente; faça backup/migre para plano durável antes do prazo. Nenhum plano pago foi contratado.
- A versão online só usará Postgres depois que `DATABASE_URL` estiver configurada. Em `GET /api/status`, procure `storage: postgres` (ou `storage: temporary_sqlite`).
- Sem `DATABASE_URL`, login e favoritos funcionam somente até o servidor gratuito reiniciar; evite usar dados reais nessa modalidade.
- Para ativar o Postgres sem compartilhar senhas: aprovar o plano de Blueprint no Render depois de revisar as mudanças, **ou** configurar `DATABASE_URL` = `Internal Database URL` pela página **Environment** do serviço e alterar o Build Command para instalar `requirements.txt`.
- O plano Render Free dorme após 15 min sem visitas. Notificações e coletas contínuas exigem job externo ou plano sempre ativo.

## Rodar local
```
cd radarpreco/src
python server.py
```
Navegue para `http://127.0.0.1:8765`. Para Postgres, instale `pip install -r radarpreco/requirements.txt` e defina `DATABASE_URL` no ambiente (não no HTML nem no Git).

## Testes
```
python -m pytest radarpreco/tests/test_alerts.py radarpreco/tests/test_http.py radarpreco/tests/test_platform9.py
python radarpreco/tests/test_screen9.py
```

Não há histórico inventado e não se promete cobertura garantida para todos os 1.000 itens do catálogo. As fontes Bright Data e Mercado Livre exigem seus próprios acessos, e SerpApi usa a chave já guardada no Render.

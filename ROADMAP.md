# Roadmap

## Concluído (MVP, Cenário A)
- [x] Buffer circular por câmera (FFmpeg), reconexão automática, limpeza e *locks*
- [x] Recorte real dos últimos N segundos pelo horário do acionamento, com validação ffprobe
- [x] Pix simulado com o mesmo fluxo do real (webhook assinado + consulta + validação)
- [x] Download/stream autorizado (Range), links temporários renováveis, expiração e exclusão
- [x] Painel admin: dashboard, pedidos/CSV, câmeras, estabelecimentos, botões, configurações, simulador, auditoria
- [x] Adaptadores Intelbras/RTSP, Mercado Pago, S3/R2 e firmware ESP32 (não validados em campo)
- [x] Demo estática para Cloudflare Pages

## Próximo (validação em campo, Cenário B)
- [ ] Validar Intelbras real: URL RTSP, GOP, conexões simultâneas, H.265 ([docs/INTELBRAS.md](docs/INTELBRAS.md))
- [ ] Validar Mercado Pago sandbox: criação de Pix, assinatura do webhook, estorno; avaliar a API de Orders
- [ ] Compilar e gravar o firmware ESP32 e testar o botão com o servidor real
- [ ] Testes E2E automatizados (Playwright) e teste em iPhone/Safari e Android reais
- [ ] Validar o build Docker em Linux (mini-PC) e o túnel HTTPS
- [ ] Textos jurídicos (LGPD, menores, direito de imagem)
- [ ] Prévia borrada/baixa resolução antes do pagamento (avaliar impacto na privacidade)

## Depois (Cenário C)
- [ ] Separar o Edge em agente com conexão de saída + fila de trabalhos
- [ ] PostgreSQL, observabilidade centralizada, multi-tenant completo
- [ ] Cupons, pacotes, assinaturas, preços promocionais
- [ ] Split de pagamento entre o estabelecimento e o Replay Shape
- [ ] Display/terminal no botão físico mostrando o código do pedido

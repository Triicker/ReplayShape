# Relatório de testes

**Data:** 27/11/2026 · **Ambiente:** Windows 11, Intel i7-8750H (6C/12T), Python 3.13.17,
FFmpeg 9.1 (gyan.dev), Chromium do painel do Claude desktop.

## 1. Testes automatizados (pytest)

```
.venv\Scripts\python -m pytest -q
57 passed in 71.69s
```

Os testes usam **FFmpeg real**. Para medir a precisão do recorte sem OCR, a fonte de teste codifica
o **segundo do vídeo na luminância** de cada quadro. O teste decodifica o primeiro e o último quadro
do replay e compara com o intervalo esperado.

| Requisito do prompt | Teste(s) | Resultado |
|---|---|---|
| Captura real dos últimos 60 s / precisão do intervalo | `test_cuts_exactly_the_last_60_seconds` (1º e último quadro dentro de ±1 s; duração 60 ±0,25 s) | ✅ |
| Integridade do MP4 / compatibilidade web | o mesmo + `test_mp4_is_web_compatible_faststart` (H.264, sem áudio, `moov` antes de `mdat`, sha256) | ✅ |
| Associação botão/QR ↔ câmera | `test_correct_camera_is_used`, `test_device_trigger_associates_camera_and_claim` | ✅ |
| Solicitações simultâneas | `test_simultaneous_requests` (4 em paralelo, locks liberados) | ✅ |
| Buffer insuficiente | `test_insufficient_buffer_fails_without_charge` | ✅ |
| Falha/lacuna de gravação | `test_gap_in_recording_is_detected` | ✅ |
| Câmera desconectada | `test_camera_offline_rejects_without_order`, `test_buffer_reports_offline_camera` (RTSP inexistente) | ✅ |
| Reconexão | `test_live_buffer_timing_and_reconnect` (mata o FFmpeg, o buffer volta sozinho) | ✅ |
| Arquivo corrompido / falha de processamento | `test_corrupted_segment_fails_cleanly` | ✅ |
| Pagamento pendente / aprovado / recusado | `test_pending_payment_blocks_download`, `test_approved_releases_correct_file`, `test_rejected_keeps_order_payable` | ✅ |
| Webhook duplicado / inválido | `test_duplicate_webhook_is_idempotent`, `test_invalid_webhook_signature_rejected` | ✅ |
| Não confiar no frontend / validação de valor | `test_frontend_cannot_fake_approval`, `test_amount_mismatch_does_not_release` | ✅ |
| Pagamento após expiração | `test_payment_after_expiration_with_file_still_present`, `test_payment_after_file_deleted_triggers_refund` | ✅ |
| Pagamento duplicado / estorno | `test_duplicate_payment_is_refunded`, `test_refund_revokes_access` | ✅ |
| Webhook perdido (conciliação) | `test_reconciliation_recovers_missed_webhook` | ✅ |
| Sem cobrança em falha | `test_no_payment_when_replay_failed`, `test_insufficient_buffer_fails_without_charge` | ✅ |
| Falha de upload | `test_upload_failure_keeps_local_copy` (3 tentativas; segue servindo do disco local) | ✅ |
| Download não autorizado / pedido de terceiros (IDOR) | `test_cannot_access_other_order`, `test_unauthorized_download_and_link_tampering` | ✅ |
| Download interrompido | `test_interrupted_download_can_resume` (Range 206 + retomada) | ✅ |
| Expiração e exclusão automática | `test_unpaid_replay_expires_and_is_deleted`, `test_paid_replay_available_then_deleted_after_window` | ✅ |
| Reinicialização do sistema | `test_restart_recovery`, `test_restart_with_missing_paid_file_flags_refund` | ✅ |
| Falta de espaço em disco | `test_low_disk_blocks_new_replays` | ✅ |
| Intervalo pelo acionamento, não pelo pagamento | `test_trigger_time_not_payment_time` | ✅ |
| Injeção no FFmpeg (marca d'água) | `test_watermark_with_hostile_text` | ✅ |
| Admin: auth, CSRF, RBAC, credenciais cifradas, SSRF, validação | `test_admin_*`, `test_rbac_operator_isolated_to_own_establishment`, `test_settings_validation`, `test_intelbras_url_and_host_validation` | ✅ |
| Botão físico: HMAC, nonce, relógio, debounce | `test_device_*`, `test_signature_matches_firmware_format` | ✅ |
| Rate limit, headers, CSV | `test_rate_limit_on_trigger`, `test_security_headers`, `test_csv_export_neutralizes_formulas` | ✅ |
| Produção bloqueia demo/mock | `test_production_refuses_mock_and_demo` | ✅ |
| Mercado Pago (formato de requisição, mapeamento, assinatura) | `test_mp_*`, **contra respostas simuladas** | ✅ (sem API real) |
| S3/R2 SigV4 | `test_s3_presign_matches_aws_reference_vector` (vetor oficial AWS) | ✅ |

### Falhas encontradas e corrigidas durante os testes
1. A fixture injetava segmentos que terminavam **antes** do acionamento, o que dava `BUFFER_TIMEOUT`.
   Corrigida para cobrir o instante do toque.
2. O teste de precisão decodificava a luminância como *limited range*. A saída `gray` do FFmpeg é
   *full range* (16–235 → 0–255). O vídeo estava correto; o erro era do teste. A decodificação foi
   corrigida.

## 2. Testes manuais (navegador do Claude desktop, servidor local)

| Cenário | Evidência | Resultado |
|---|---|---|
| Câmera simulada + buffer em tempo real | painel: 2 câmeras `online`, buffer 120 s / 60 segmentos | ✅ |
| Botão desabilitado enquanto o buffer < 60 s ("Aquecendo… 45 s") | tela da mesa | ✅ |
| Fluxo completo em viewport 375×812: mesa → processamento → Pix → simular aprovado → player | replay de 60,0 s 1280×720 carregado no `<video>`; link de download com token | ✅ |
| Precisão visual do recorte (relógio gravado na imagem) | janela 18:39:01,30 → 18:39:09,30; 1º quadro mostra 18:39:01, último 18:39:09 | ✅ |
| Tempo de processamento (60 s, 720p, recodificação `veryfast`) | 5,3 s (i7-8750H, cena sintética) | medido, 1 amostra |
| Console sem erros (inclui CSP) | `read_console_messages` vazio | ✅ |
| Painel admin: login, dashboard, câmeras, simulador | capturas de tela | ✅ |
| Demo estática (dist/pages) fluxo completo | pedido → "Pagamento aprovado" → vídeo de 60 s | ✅ |

## 3. O que NÃO foi testado (limitações)

- **Safari/iOS e Android reais**: não testados. Usamos MP4 H.264 + faststart, `playsinline` e Web
  Share nível 2 com preparo prévio do arquivo (exigência do iOS), mas **não há evidência em
  aparelho real**. O teste foi feito só no Chromium, com emulação de viewport.
- **Câmeras Intelbras reais / RTSP real**: apenas a montagem da URL e o caminho de falha (RTSP
  inexistente). Ver [INTELBRAS.md](INTELBRAS.md).
- **Mercado Pago sandbox/produção**: nenhum request real. O template da assinatura do webhook está
  pendente de confirmação.
- **Cloudflare R2 real**: só o algoritmo de assinatura (vetor AWS). Upload real não executado.
- **Firmware ESP32**: não compilado nem gravado.
- **Docker**: o Docker Desktop estava instalado, mas o daemon não estava em execução, então o
  build da imagem **não foi validado**.
- **Playwright E2E**: não configurado. A verificação de interface foi manual, pelo navegador
  integrado.
- **Capacidade** (câmeras por mini-PC, replays simultâneos): sem benchmark. As estimativas em
  DEPLOYMENT.md são de projeto.

# Guia de configuração — câmeras Intelbras

> **Nada neste guia foi testado em equipamento Intelbras real** (não havia equipamento disponível).
> Os padrões abaixo são os mais comuns e **precisam ser validados** em cada modelo/firmware.

> **Câmeras analógicas (HDCVI), como a VHL 1120 B (720p):** não têm IP. O sistema lê o vídeo pelo
> **DVR**, no canal em que a câmera está ligada. O assistente `configurar_camera.bat` encontra o DVR,
> testa os canais e mostra uma foto de cada um (ver [PILOTO.md](PILOTO.md)).

## 1. Pré-requisitos no equipamento
1. Câmera IP ou NVR/DVR na **mesma rede local** do Edge (mini-PC). **Não** faça redirecionamento de
   portas para a internet.
2. Crie um **usuário só de visualização** para o Replay Shape. Não use o `admin` do equipamento.
3. Na configuração de vídeo do canal:
   - Codificação **H.264** (preferível). H.265 funciona no buffer, mas cada replay é recodificado para H.264.
   - **Intervalo de I-frame (GOP) = FPS** (1 keyframe por segundo) ou no máximo 2× FPS. O buffer
     segmenta em keyframes; GOP longo deixa os segmentos irregulares e aumenta a latência.
   - Resolução 1080p ou 720p, 25/30 fps, bitrate de 2–4 Mbps.
4. Confira o **limite de conexões RTSP simultâneas** do modelo. O buffer abre 1 conexão por câmera,
   além das que o NVR/app já usam.

## 2. URL RTSP
O adaptador `intelbras` monta:

```
rtsp://USUARIO:SENHA@IP:554/cam/realmonitor?channel=N&subtype=S
```

`channel` é o canal no NVR/DVR (câmera IP avulsa = 1). `subtype` 0 é o stream principal, 1 é o extra.
Se o seu modelo usar outro caminho, preencha **Caminho RTSP** ou **URL RTSP completa** no cadastro
(o adaptador `rtsp` genérico aceita qualquer URL de rede local).

### Validar antes de cadastrar
No mini-PC:
```powershell
ffprobe -rtsp_transport tcp -i "rtsp://usuario:senha@192.168.1.108:554/cam/realmonitor?channel=1&subtype=0"
```
O resultado esperado é uma trilha `Video: h264` com resolução e fps. Depois, no painel, use
**Câmeras > Testar**.

## 3. Cadastro no painel
*Câmeras > Nova câmera*: adaptador **Intelbras**, IP, porta 554, usuário, senha, canal e stream.
A senha é criptografada e nunca é exibida de novo. Depois de salvar, o buffer inicia e o status
muda para `online` em poucos segundos. Use **Replay teste** para gerar um vídeo sem cobrança.

## 4. Critérios de aceite (equipamento real)
- [ ] `ffprobe` lê o stream; **Testar** retorna OK
- [ ] Status `online` estável por 1 h, com reinícios = 0 ou baixos
- [ ] Replay de 60 s gerado com relógio do OSD da câmera coerente com o horário do toque (±1 s)
- [ ] Derrubar a câmera (desligar o cabo) → status `offline` → religar → volta a `online` sozinha
- [ ] Gravação original do NVR **não** é afetada (conferir no próprio NVR)
- [ ] CPU/RAM do mini-PC com todas as câmeras ligadas (registrar em `docs/TEST_REPORT.md`)

## 5. Estratégia B — gravação histórica (opcional)
Alguns NVRs aceitam reprodução por intervalo:
```
rtsp://USUARIO:SENHA@IP:554/cam/playback?channel=1&starttime=2026_11_27_18_00_00&endtime=2026_11_27_18_01_00
```
Teste com `ffprobe`/`ffmpeg -t 60` usando um intervalo já gravado. **Somente se funcionar**, marque
"Playback histórico validado" na câmera. Assim, quando o buffer estiver indisponível, o sistema
tenta recuperar o trecho do NVR. Também vale avaliar ONVIF Profile G e o SDK oficial Intelbras como
alternativas. A compatibilidade **não** é presumida.

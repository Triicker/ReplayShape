# Guia do piloto: câmera real, Pix de teste

O piloto roda no **computador do estabelecimento** (ou num notebook levado até lá) ligado na
mesma rede do DVR. Ele usa a câmera real e o Pix fica **simulado**: nenhum valor é cobrado.

Serve para:
- **DVR Intelbras com câmeras analógicas HDCVI**, como a **VHL 1120 B**. Essas câmeras não têm IP
  próprio; o vídeo vem do canal do DVR.
- **Câmeras IP** Intelbras ou RTSP genéricas.

## Antes de ir ao local
- Computador com **Windows 10/11 64 bits**, 4 GB de RAM livres e 5 GB livres no disco, **na mesma
  rede do DVR**, de preferência por cabo.
- **Usuário e senha do DVR**. É o mesmo login do monitor do DVR ou do app iSIC. Se puder, crie um
  usuário só de visualização para o Replay Shape.
- O IP do DVR é opcional, porque o assistente procura sozinho. Se precisar, ele aparece no menu
  **Rede** do DVR.
- Python 3.12 instalado. No **Prompt de Comando**: `winget install Python.Python.3.12`.

## Passo a passo
1. **Baixe o projeto** (Code > Download ZIP no GitHub, ou `git clone`) e extraia numa pasta, por
   exemplo `C:\ReplayShape`.
2. Dê dois cliques em **`instalar.bat`**. Ele instala as dependências e o FFmpeg. Se ele pedir,
   feche a janela e rode de novo.
3. Dê dois cliques em **`configurar_camera.bat`**:
   - Ele procura DVRs e câmeras na rede (porta RTSP 554) e mostra a lista.
   - Pede o usuário e a **senha** (não aparece enquanto você digita). Cuidado: o DVR pode
     bloquear depois de várias senhas erradas.
   - Se for DVR, testa os canais 1 a 16, salva **uma foto de cada canal** e abre a pasta. Escolha o
     canal da câmera que filma a quadra.
   - Mede codec, resolução e intervalo de keyframes, e avisa se precisar ajustar algo.
   - Pergunta o nome do local, da quadra, o preço e a duração, e cadastra.
4. Dê dois cliques em **`iniciar_piloto.bat`** e escolha:
   - **1) Wi-Fi do local**: o celular precisa estar no mesmo Wi-Fi do computador. Se o Windows
     perguntar sobre o Firewall, permita em **Redes privadas**.
   - **2) Internet/4G**: cria um link **HTTPS** temporário pela Cloudflare (gratuito, sem conta).
     Exige instalar uma vez: `winget install Cloudflare.cloudflared`. Com HTTPS, o botão
     **Compartilhar** do celular consegue enviar o arquivo direto.
5. A janela mostra o **link** e um **QR Code** de cada quadra. Escaneie com o celular.
   **Espere ~60 s** depois de iniciar: o botão fica em "Aquecendo…" até o primeiro minuto ser gravado.
6. Toque em **GERAR MEU REPLAY**. Na tela do Pix, use **"Simular aprovado"** e depois assista,
   baixe e compartilhe.
7. O **painel admin** fica no link mostrado + `/admin`. A senha inicial está em
   `data\piloto\ADMIN_INITIAL_PASSWORD.txt`. Lá você vê os pedidos, o status da câmera, imprime o QR
   da quadra (botão **QR**) e ajusta preço e duração.

**A janela precisa ficar aberta.** Enquanto ela roda, o computador não entra em suspensão (a tela
pode apagar). Para abrir sozinho ao ligar o PC, rode **`inicio_automatico.bat`**. Rode de novo para
desligar.

## Ajustes recomendados no DVR (melhoram muito o resultado)
No DVR: **Menu > Câmera (ou Codificação) > stream principal do canal da quadra**:
- **Compressão**: H.264. H.265 funciona, mas o recorte gasta mais CPU.
- **H.264+ / H.265+ (compressão inteligente)**: **desligado**. Ela espaça os keyframes e atrasa o replay.
- **Intervalo de I-frame (GOP)**: igual ao FPS (ex.: 25 fps → 25).
- **RTSP**: habilitado (Rede > Conexões/RTSP, porta 554). Normalmente já vem assim.

O assistente mede o intervalo de keyframes e avisa se estiver longo. O sistema funciona mesmo
assim, porque se adapta a GOP longo, mas o replay demora mais para ficar pronto.

## O que observar no teste (anote para ajustarmos)
- [ ] O vídeo do replay corresponde aos **60 s antes do toque**? Compare com o relógio que o DVR
      grava na imagem.
- [ ] Quanto tempo do toque até "Seu replay está pronto!"?
- [ ] A câmera fica **online** por horas? Veja *Admin > Câmeras*: status, segundos de buffer e reinícios.
- [ ] A gravação normal do DVR continua funcionando enquanto o piloto roda?
- [ ] O vídeo toca, baixa e compartilha no **iPhone** e no **Android**?
- [ ] Qualidade de imagem e tamanho do arquivo (720p ≈ 3–11 MB por minuto, conforme o movimento).

## Problemas comuns
| Mensagem | O que fazer |
|---|---|
| `não respondeu` | O PC não está na rede do DVR ou o IP está errado. Teste com `ping IP-DO-DVR`. |
| `Usuário ou senha recusados` | Confira no próprio DVR. Se bloqueou, espere uns 30 min. |
| `Nenhum canal respondeu` | Habilite o RTSP no DVR (Rede > Conexões). Confira se a porta é 554. |
| Botão em "Aquecendo…" por muito tempo | Veja *Admin > Câmeras*. Se o status for `offline`, o detalhe mostra o erro. |
| Celular não abre o link | No modo Wi-Fi: mesma rede? O Firewall liberou o Python? Tente a opção 2 (internet). |
| `CAMERA_OFFLINE` ao gerar | O DVR recusou mais conexões. Feche outros visualizadores ou reinicie o DVR. |

## Limitações do piloto
- **Pix simulado.** Para cobrar de verdade, configure o Mercado Pago (`docs/MERCADOPAGO.md`).
- O link da opção 2 **muda a cada início**. Para um endereço fixo, use um domínio próprio com
  Cloudflare Tunnel nomeado (ver `DEPLOYMENT.md`).
- Este fluxo ainda **não foi testado com um DVR real**. A descoberta, o cadastro e o buffer foram
  testados com câmera simulada e com aparelhos inexistentes (erro tratado).

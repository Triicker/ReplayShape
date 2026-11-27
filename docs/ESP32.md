# Guia — botão físico ESP32

> Firmware em `firmware/esp32/replay_button/`. **Não foi compilado nem gravado em placa** nesta
> entrega. O algoritmo de assinatura é o mesmo do backend e está coberto por teste
> (`test_signature_matches_firmware_format`).

## Hardware
- ESP32 DevKit (qualquer modelo com Wi-Fi), botão arcade/industrial de contato seco e fonte 5 V.
- Ligação: botão entre **GPIO 13** e **GND** (usa pull-up interno). O LED é o onboard (GPIO 2) ou
  um LED externo com resistor.
- Instale em caixa à prova de respingos, perto da quadra/mesa e fora do alcance de bolas.

## Configuração
1. No painel: *Botões físicos > Cadastrar botão* (escolha a câmera). Copie `DEVICE_ID`,
   `DEVICE_SECRET` e `API_BASE`. **O segredo aparece uma única vez.**
2. Copie `config.example.h` para `config.h` (ignorado pelo Git) e preencha o Wi-Fi e as credenciais.
3. Em produção (HTTPS), cole o certificado raiz em `API_ROOT_CA`. Sem ele, o firmware usa
   `setInsecure()`, que é **aceitável só em testes**.
4. Arduino IDE ou `arduino-cli`: placa "ESP32 Dev Module", compile e grave.

## Comportamento
| LED | Significado |
|---|---|
| aceso fixo | pronto (Wi-Fi + servidor OK) |
| piscando durante a conexão | conectando ao Wi-Fi |
| 3 piscadas longas | replay solicitado com sucesso |
| piscadas rápidas | erro (sem rede, relógio NTP não sincronizado, servidor recusou) |
| 2 piscadas curtas | ainda em cooldown local |

- Antirrepique por software (40 ms) e cooldown local de 10 s. O servidor também aplica cooldown
  por dispositivo (padrão 15 s) e responde `debounced`.
- O heartbeat a cada 60 s alimenta a coluna *Conectividade* do painel.

## Como o cliente recebe o replay do botão
O botão não tem tela. Na página do QR da mesa, o cliente toca em **"Apertei o botão da mesa"** e
recebe o acionamento mais recente ainda não reivindicado daquela câmera (janela configurável, padrão
3 min; o primeiro a reivindicar fica com ele). Também é possível exibir o código do pedido
(`short_code`, devolvido pela API) em um display opcional e usar **"Tenho um código"**.

## Critérios de aceite
- [ ] Compila e conecta ao Wi-Fi; relógio sincronizado via NTP
- [ ] Toque → `accepted` no monitor serial → pedido aparece no painel com origem `device`
- [ ] Toques repetidos em < 15 s → `debounced` (1 replay só)
- [ ] Segredo errado → 401; heartbeat atualiza "online" no painel
- [ ] Queda de Wi-Fi → reconecta sozinho

// Copie para config.h (ignorado pelo Git) e preencha. NUNCA versione config.h.
#pragma once

#define WIFI_SSID      "NOME_DA_REDE"
#define WIFI_PASSWORD  "SENHA_DO_WIFI"

// Obtidos no painel: Botões físicos > Cadastrar botão (o segredo aparece uma única vez)
#define DEVICE_ID      "id-de-32-caracteres-hex"
#define DEVICE_SECRET  "segredo-de-64-caracteres-hex"
#define API_BASE       "http://192.168.1.10:8000"   // ou https://seu-dominio

// Em produção (HTTPS), cole o certificado raiz da sua CA e descomente:
// #define API_ROOT_CA "-----BEGIN CERTIFICATE-----\n...\n-----END CERTIFICATE-----\n"

#define BUTTON_PIN        13      // botão entre o GPIO e GND (pull-up interno)
#define LED_PIN           2       // LED onboard na maioria das placas DevKit
#define DEBOUNCE_MS       40
#define LOCAL_COOLDOWN_MS 10000   // o servidor também aplica cooldown
#define HEARTBEAT_MS      60000

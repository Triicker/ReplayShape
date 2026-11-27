/*
  Replay Shape — botão físico (ESP32, Arduino core 2.x/3.x)

  Funções:
  - Conecta ao Wi-Fi (reconexão automática) e sincroniza o relógio via NTP.
  - Ao pressionar o botão, envia POST autenticado (HMAC-SHA256) para
    /api/v1/internal/devices/{DEVICE_ID}/trigger.
  - Antirrepique por software + bloqueio local (cooldown); o servidor também
    aplica cooldown e rejeita nonces repetidos.
  - LED: aceso fixo = pronto; piscando rápido = enviando; 3 piscadas longas = OK;
    piscadas curtas contínuas = erro/sem rede.
  - Heartbeat a cada 60 s (monitoramento de conectividade no painel).

  Assinatura (igual a backend/replayshape/security.py::device_signature):
    msg = ts + "\n" + nonce + "\n" + "POST" + "\n" + path + "\n" + sha256_hex(body)
    X-RS-Signature = hex(HMAC_SHA256(DEVICE_SECRET, msg))

  STATUS: firmware NÃO compilado/testado em hardware neste repositório
  (sem placa disponível). O formato da assinatura é coberto por teste no backend.
*/
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <HTTPClient.h>
#include <time.h>
#include "mbedtls/md.h"
#include "config.h"   // copie config.example.h para config.h e preencha

#ifndef FIRMWARE_VERSION
#define FIRMWARE_VERSION "rs-btn-0.1.0"
#endif

static unsigned long lastPressMs = 0;
static unsigned long lastHeartbeatMs = 0;
static bool lastLevel = HIGH;
static unsigned long levelChangedMs = 0;

// ---------- utilitários ----------------------------------------------------
static String toHex(const uint8_t *buf, size_t len) {
  static const char *hex = "0123456789abcdef";
  String s; s.reserve(len * 2);
  for (size_t i = 0; i < len; i++) { s += hex[buf[i] >> 4]; s += hex[buf[i] & 0xF]; }
  return s;
}

static String sha256Hex(const String &data) {
  uint8_t out[32];
  mbedtls_md_context_t ctx;
  mbedtls_md_init(&ctx);
  mbedtls_md_setup(&ctx, mbedtls_md_info_from_type(MBEDTLS_MD_SHA256), 0);
  mbedtls_md_starts(&ctx);
  mbedtls_md_update(&ctx, (const uint8_t *)data.c_str(), data.length());
  mbedtls_md_finish(&ctx, out);
  mbedtls_md_free(&ctx);
  return toHex(out, 32);
}

static String hmacSha256Hex(const String &key, const String &msg) {
  uint8_t out[32];
  mbedtls_md_context_t ctx;
  mbedtls_md_init(&ctx);
  mbedtls_md_setup(&ctx, mbedtls_md_info_from_type(MBEDTLS_MD_SHA256), 1);
  mbedtls_md_hmac_starts(&ctx, (const uint8_t *)key.c_str(), key.length());
  mbedtls_md_hmac_update(&ctx, (const uint8_t *)msg.c_str(), msg.length());
  mbedtls_md_hmac_finish(&ctx, out);
  mbedtls_md_free(&ctx);
  return toHex(out, 32);
}

static String randomNonce() {
  uint8_t b[12];
  for (int i = 0; i < 12; i++) b[i] = (uint8_t)(esp_random() & 0xFF);
  return toHex(b, 12);
}

static void blink(int times, int onMs, int offMs) {
  for (int i = 0; i < times; i++) {
    digitalWrite(LED_PIN, HIGH); delay(onMs);
    digitalWrite(LED_PIN, LOW); delay(offMs);
  }
}

// ---------- rede -------------------------------------------------------------
static bool ensureWifi() {
  if (WiFi.status() == WL_CONNECTED) return true;
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  unsigned long start = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - start < 15000) {
    digitalWrite(LED_PIN, !digitalRead(LED_PIN));
    delay(150);
  }
  return WiFi.status() == WL_CONNECTED;
}

static bool clockReady() { return time(nullptr) > 1700000000; }

static int signedPost(const char *suffix, String &response) {
  if (!ensureWifi() || !clockReady()) return -1;
  String path = String("/api/v1/internal/devices/") + DEVICE_ID + "/" + suffix;
  String body = "";
  String ts = String((long)time(nullptr));
  String nonce = randomNonce();
  String msg = ts + "\n" + nonce + "\nPOST\n" + path + "\n" + sha256Hex(body);
  String sig = hmacSha256Hex(DEVICE_SECRET, msg);

  HTTPClient http;
  WiFiClientSecure secure;
  WiFiClient plain;
  String url = String(API_BASE) + path;
  if (url.startsWith("https://")) {
#ifdef API_ROOT_CA
    secure.setCACert(API_ROOT_CA);       // recomendado em produção
#else
    secure.setInsecure();                 // SOMENTE para testes — ver docs/ESP32.md
#endif
    http.begin(secure, url);
  } else {
    http.begin(plain, url);               // rede local / desenvolvimento
  }
  http.setTimeout(8000);
  http.addHeader("X-RS-Timestamp", ts);
  http.addHeader("X-RS-Nonce", nonce);
  http.addHeader("X-RS-Signature", sig);
  http.addHeader("X-RS-Firmware", FIRMWARE_VERSION);
  int code = http.POST(body);
  response = code > 0 ? http.getString() : String("");
  http.end();
  return code;
}

// ---------- ciclo principal ---------------------------------------------------
void setup() {
  Serial.begin(115200);
  pinMode(LED_PIN, OUTPUT);
  pinMode(BUTTON_PIN, INPUT_PULLUP);  // botão entre o pino e GND
  digitalWrite(LED_PIN, LOW);
  ensureWifi();
  configTime(0, 0, "pool.ntp.org", "time.google.com");
  unsigned long start = millis();
  while (!clockReady() && millis() - start < 15000) delay(200);
  Serial.printf("[replay-shape] pronto. wifi=%d ntp=%d\n", WiFi.status() == WL_CONNECTED, clockReady());
  digitalWrite(LED_PIN, HIGH);
}

void loop() {
  // Antirrepique: o nível precisa ficar estável por DEBOUNCE_MS.
  bool level = digitalRead(BUTTON_PIN);
  if (level != lastLevel) { lastLevel = level; levelChangedMs = millis(); }
  static bool armed = true;
  if (level == LOW && armed && millis() - levelChangedMs > DEBOUNCE_MS) {
    armed = false;
    if (millis() - lastPressMs > LOCAL_COOLDOWN_MS || lastPressMs == 0) {
      lastPressMs = millis();
      digitalWrite(LED_PIN, LOW);
      String resp;
      int code = signedPost("trigger", resp);
      Serial.printf("[replay-shape] trigger -> %d %s\n", code, resp.c_str());
      if (code == 200) blink(3, 400, 200); else blink(10, 80, 80);
      digitalWrite(LED_PIN, HIGH);
    } else {
      blink(2, 60, 60);  // ainda em cooldown
      digitalWrite(LED_PIN, HIGH);
    }
  }
  if (level == HIGH) armed = true;

  if (millis() - lastHeartbeatMs > HEARTBEAT_MS) {
    lastHeartbeatMs = millis();
    String resp;
    int code = signedPost("heartbeat", resp);
    digitalWrite(LED_PIN, code == 200 ? HIGH : LOW);
    if (code != 200) Serial.printf("[replay-shape] heartbeat falhou: %d\n", code);
  }
  delay(5);
}

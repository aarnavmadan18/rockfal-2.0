/*
  INNOVATRIX Sensor Node — ESP32
  Reads real vibration + tilt (MPU6050), rainfall (analog rain sensor),
  temperature/humidity (DHT22), and POSTs JSON to the backend every few
  seconds over WiFi.

  Libraries needed (Arduino Library Manager):
    - Adafruit MPU6050
    - Adafruit Unified Sensor
    - DHT sensor library (Adafruit)
    - ArduinoJson

  Wiring:
    MPU6050  -> ESP32:  VCC->3V3, GND->GND, SDA->GPIO21, SCL->GPIO22
    Rain sensor (analog out) -> GPIO34
    DHT22 data -> GPIO27 (with 10k pull-up to 3V3)

  Set ZONE_ID to match one of the backend zones: A, B, C, D, E, F
  (each physical node you deploy = one zone).
*/

#include <WiFi.h>
#include <HTTPClient.h>
#include <Wire.h>
#include <Adafruit_MPU6050.h>
#include <Adafruit_Sensor.h>
#include <DHT.h>
#include <ArduinoJson.h>

// ---------------- CONFIG ----------------
const char* WIFI_SSID     = "YOUR_WIFI_SSID";
const char* WIFI_PASSWORD = "YOUR_WIFI_PASSWORD";
const char* SERVER_URL    = "http://192.168.1.50:8000/api/sensor-data"; // <-- your backend machine's LAN IP
const char* ZONE_ID       = "A"; // change per physical node: A / B / C / D / E / F

#define RAIN_PIN   34
#define DHT_PIN    27
#define DHT_TYPE   DHT22
#define SEND_INTERVAL_MS 3000
#define VIB_SAMPLE_WINDOW_MS 500   // collect accel samples for this long, then send peak-to-RMS magnitude

Adafruit_MPU6050 mpu;
DHT dht(DHT_PIN, DHT_TYPE);

unsigned long lastSend = 0;

void connectWiFi() {
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  Serial.print("Connecting to WiFi");
  while (WiFi.status() != WL_CONNECTED) {
    delay(400);
    Serial.print(".");
  }
  Serial.println("\nConnected. IP: " + WiFi.localIP().toString());
}

void setup() {
  Serial.begin(115200);
  connectWiFi();

  Wire.begin();
  if (!mpu.begin()) {
    Serial.println("MPU6050 not found - check wiring!");
    while (1) delay(1000);
  }
  mpu.setAccelerometerRange(MPU6050_RANGE_4_G);
  mpu.setGyroRange(MPU6050_RANGE_500_DEG);
  mpu.setFilterBandwidth(MPU6050_BAND_21_HZ);

  dht.begin();
  pinMode(RAIN_PIN, INPUT);

  Serial.println("INNOVATRIX node ready. Zone: " + String(ZONE_ID));
}

// Sample the accelerometer for VIB_SAMPLE_WINDOW_MS and return
// gravity-removed RMS magnitude (m/s^2) as the "vibration" reading,
// plus tilt angle (degrees) from the gravity vector.
void readMotion(float &vibrationOut, float &tiltOut) {
  unsigned long start = millis();
  double sumSq = 0;
  int n = 0;
  double gx = 0, gy = 0, gz = 0; // running average for tilt reference

  while (millis() - start < VIB_SAMPLE_WINDOW_MS) {
    sensors_event_t a, g, temp;
    mpu.getEvent(&a, &g, &temp);

    double mag = sqrt(a.acceleration.x * a.acceleration.x +
                       a.acceleration.y * a.acceleration.y +
                       a.acceleration.z * a.acceleration.z);
    double dynamic = mag - 9.81; // remove gravity baseline
    sumSq += dynamic * dynamic;
    gx += a.acceleration.x; gy += a.acceleration.y; gz += a.acceleration.z;
    n++;
    delay(10);
  }
  if (n == 0) n = 1;
  vibrationOut = sqrt(sumSq / n);

  gx /= n; gy /= n; gz /= n;
  // tilt = angle between measured gravity vector and vertical (z) axis
  double normXY = sqrt(gx * gx + gy * gy);
  tiltOut = atan2(normXY, gz) * 180.0 / PI;
}

float readRainfallMM() {
  // Analog rain sensor: wetter board -> lower resistance -> lower voltage.
  // Map raw ADC (0-4095) to a rough "mm-equivalent" intensity scale for the demo.
  int raw = analogRead(RAIN_PIN);
  float wetness = 1.0 - (raw / 4095.0); // 0 = dry, 1 = fully wet
  return wetness * 50.0; // scale to 0-50mm range matching backend normalization
}

void sendReading(float vibration, float tilt, float rainfall, float temperature, float humidity) {
  if (WiFi.status() != WL_CONNECTED) {
    connectWiFi();
  }

  HTTPClient http;
  http.begin(SERVER_URL);
  http.addHeader("Content-Type", "application/json");

  StaticJsonDocument<256> doc;
  doc["zone_id"] = ZONE_ID;
  doc["vibration"] = vibration;
  doc["tilt"] = tilt;
  doc["rainfall"] = rainfall;
  doc["temperature"] = temperature;
  doc["humidity"] = humidity;

  String payload;
  serializeJson(doc, payload);

  int code = http.POST(payload);
  Serial.printf("POST -> %d | vib=%.2f tilt=%.2f rain=%.1f temp=%.1f hum=%.1f\n",
                code, vibration, tilt, rainfall, temperature, humidity);
  http.end();
}

void loop() {
  if (millis() - lastSend >= SEND_INTERVAL_MS) {
    lastSend = millis();

    float vibration, tilt;
    readMotion(vibration, tilt);
    float rainfall = readRainfallMM();
    float temperature = dht.readTemperature();
    float humidity = dht.readHumidity();
    if (isnan(temperature)) temperature = 25.0;
    if (isnan(humidity)) humidity = 50.0;

    sendReading(vibration, tilt, rainfall, temperature, humidity);
  }
}

#include <Servo.h>

Servo LServo; // left hand servo
Servo RServo; // right hand servo
Servo HServo; // head servo

const int LS_pin = 8;  // left hand servo pin    
const int HS_pin = 9;  // head servo pin
const int RS_pin = 10; // right hand servo pin

void setup() {
  // Initialize Serial communication at 9600 baud rate
  Serial.begin(9600);

  // Attach the servos to their respective pins
  LServo.attach(LS_pin);
  RServo.attach(RS_pin);
  HServo.attach(HS_pin);
  
  // Set initial default positions
  LServo.write(180);
  HServo.write(90);
  RServo.write(0);
}

void loop() {
  // The Python script sends a 10-character string like: "$180000090"
  // It starts with '$' and has exactly 3 digits for each of the 3 servos.
  
  // Wait until we have a full command (10 bytes) in the buffer
  if (Serial.available() >= 10) {
    
    // Check if the first character is our start marker
    if (Serial.read() == '$') {
      
      // Since we already verified there are >= 10 bytes in the buffer,
      // and we just read the 1st byte ('$'), we know the next 9 bytes are ready to read instantly!
      
      char s1[4];
      s1[0] = Serial.read();
      s1[1] = Serial.read();
      s1[2] = Serial.read();
      s1[3] = '\0'; 
      
      char s2[4];
      s2[0] = Serial.read();
      s2[1] = Serial.read();
      s2[2] = Serial.read();
      s2[3] = '\0';
      
      char s3[4];
      s3[0] = Serial.read();
      s3[1] = Serial.read();
      s3[2] = Serial.read();
      s3[3] = '\0';
      
      // Tell the servos to move to those positions!
      LServo.write(atoi(s1)); // Left Hand
      HServo.write(atoi(s2)); // Head
      RServo.write(atoi(s3)); // Right Hand
      
      // Clear any leftover junk in the buffer up to this point
      while (Serial.available() > 0 && Serial.peek() != '$') {
        Serial.read();
      }
    }
  }
}

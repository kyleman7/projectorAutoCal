#!/bin/bash
# bdkey.sh <code> <description>  — Samsung BD SendKeyCode over UPnP
curl -s -m 5 -X POST http://192.168.1.102:7676/smp_5_ \
 -H 'Content-Type: text/xml; charset="utf-8"' \
 -H 'SOAPACTION: "urn:samsung.com:service:MultiScreenService:1#SendKeyCode"' \
 --data "<?xml version=\"1.0\" encoding=\"utf-8\"?><s:Envelope xmlns:s=\"http://schemas.xmlsoap.org/soap/envelope/\" s:encodingStyle=\"http://schemas.xmlsoap.org/soap/encoding/\"><s:Body><u:SendKeyCode xmlns:u=\"urn:samsung.com:service:MultiScreenService:1\"><KeyCode>$1</KeyCode><KeyDescription>$2</KeyDescription></u:SendKeyCode></s:Body></s:Envelope>"
echo

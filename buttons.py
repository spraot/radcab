#!/usr/bin/env python3

#MQTT format based on
#https://www.home-assistant.io/docs/mqtt/discovery/

#MQTT lib
#https://pypi.org/project/paho-mqtt/

#md-icon see
#https://cdn.materialdesignicons.com/4.5.95/

import os
import sys
import json
import yaml
import traceback
import revpimodio2
import paho.mqtt.client as mqtt
import time
import threading
import logging
import atexit
from itertools import chain, combinations

class ButtonControl():
    config_file = 'config.yml'
    topic_prefix = 'pi/io'
    homeassistant_prefix = 'homeassistant'
    mqtt_server_ip = "localhost"
    mqtt_server_port = 1883
    mqtt_server_user = ""
    mqtt_server_password = ""
    R0 = 100
    max_readings = 2
    eq_readings = 2
    V_nom = 3.365
    V_acc = 15

    default_button = {
    }

    buttons = {}
    groups = []
    channels = {}

    def __init__(self):
        logging.basicConfig(level=os.environ.get("LOGLEVEL", "INFO"), format='%(asctime)s;<%(levelname)s>;%(message)s')
        logging.info("Init")

        if len(sys.argv) > 1:
            self.config_file = sys.argv[1]

        self.load_config()
        self.init_channels()

        #MQTT init
        self.mqttclient = mqtt.Client()
        self.mqttclient.on_connect = self.mqtt_on_connect

        #RPI init
        self.rpi = revpimodio2.RevPiModIO(autorefresh=True, direct_output=True, configrsc='/config.rsc')
        self.rpi.handlesignalend(self.programend)

         #Register program end event
        atexit.register(self.programend)

        logging.info("init done")

    def load_config(self):
        logging.info("Reading config from "+self.config_file)

        with open(self.config_file, 'r') as f:
            config = yaml.safe_load(f)

        for key in ['topic_prefix', 'homeassistant_prefix', 'mqtt_server_ip', 'mqtt_server_port', 'mqtt_server_user', 'mqtt_server_password', 'R0', 'buttons', 'groups']:
            try:
                self.__setattr__(key, config[key])
            except KeyError:
                pass

        for name, button in self.buttons.items():
            button['id'] = name

            if not 'output_id' in button:
                button['output_id'] = button["id"]

            for k, v in self.default_button.items():
                if not k in button:
                    button[k] = v

            if not 'output_id' in button:
                button['unique_id'] = button["id"]

            button["mqtt_config_topic"] = "{}/{}/{}/config".format(self.homeassistant_prefix, 'switch', button["id"])
            button["mqtt_state_topic"] = "{}/{}/state".format(self.topic_prefix, button["id"])
            button["mqtt_availability_topic"] = "{}/{}/availability".format(self.topic_prefix, button["id"])

        self.groups = [[self.buttons[b] for b in group] for group in self.groups]

    def init_channels(self):
        def get_channel(id):
            try:
                return self.channels[id]
            except KeyError:
                self.channels[id] = {'id': id, 'V': [], 'buttons': []}
                add_v(id, [1e20], ())
            return self.channels[id]

        def add_v(channel_id, r, b):
            if len(r) == 1:
                r = r[0]
            else:
                r = 1/sum(1/x for x in r)

            v_int = max(0, min(self.V_nom*1000, (1-r/(self.R0+r))*self.V_nom))*1000

            get_channel(channel_id)['V'].append((v_int, b))

            if not b:
                b_str = 'no buttons are'
            elif len(b) == 1:
                b_str = 'button {} is'.format(b[0]['id'])
            else:
                b_str = 'buttons {} are'.format(', '.join(x['id'] for x in b))

            logging.info('Expect {:0.3f}V on channel {} when {} pressed'.format(v_int*0.001, channel_id, b_str))


        for button in self.buttons.values():
            add_v(button['channel'], [button['R']], (button, ))
            get_channel(button['channel'])['buttons'].append(button)
            button['down'] = False

        for group in self.groups:
            if len(set([b['channel'] for b in group])) > 1:
                logging.error('All buttons in group must have the same channel, skipping processing of group '+str(group))
            else:
                for combo in chain.from_iterable(combinations(group, r) for r in range(len(group)+1)):
                    if len(combo) > 1:
                        add_v(combo[0]['channel'], [b['R'] for b in combo], combo)

        for channel in self.channels.values():
            channel['cur_reading'] = 0
            channel['readings'] = [0]*self.max_readings
            channel['state'] = [False]*len(channel['buttons'])

    def configure_mqtt_for_button(self, button):
        button_configuration = {
            "state_topic": button["mqtt_state_topic"],
            "availability_topic": button["mqtt_availability_topic"],
            "retain": False,
            "device": {"identifiers": button["id"]}
        }

        try:
            button_configuration['name'] = button["name"]
        except KeyError:
            button_configuration['name'] = button["id"]

        button_configuration['device']['name'] = button_configuration["name"]

        try:
            button_configuration['unique_id'] = button["unique_id"]
        except KeyError:
            button_configuration['unique_id'] = button["id"]

        try:
            button_configuration['icon'] = "mdi:" + button["md-icon"]
        except KeyError:
            pass

        json_conf = json.dumps(c)
        logging.debug("Broadcasting homeassistant configuration for button: " + button["name"] + ":" + json_conf)
        self.mqttclient.publish(button["mqtt_config_topic"], payload=json_conf, qos=0, retain=True)
        
    def start(self):
        logging.info("starting")

        #MQTT startup
        logging.info("Starting MQTT client")
        self.mqttclient.username_pw_set(self.mqtt_server_user, password=self.mqtt_server_password)
        self.mqttclient.connect(self.mqtt_server_ip, self.mqtt_server_port, 60)
        self.mqttclient.loop_start()
        logging.info("MQTT client started")

        logging.info("started")
        self.rpi.cycleloop(self.cycleloop, cycletime=15)

    def cycleloop(self, ct):
        for channel in self.channels.values():
            v = self.read_channel(channel['id'])

            closestV = 0 if v < 150 else -1
            for i in range(len(channel['V'])):
                if abs(v-channel['V'][i][0]) < self.V_acc \
                and (closestV == -1 or abs(channel['V'][closestV][0]-channel['V'][i][0])):
                    closestV = i

            channel['readings'][channel['cur_reading']] = closestV

            if channel['id'] == 'AI_1' and v > 150:
                logging.debug('on channel {} read value {}, closest buttons: {}'.format(channel['id'], v, ', '.join(x['id'] for x in channel['V'][closestV][1])))

            if closestV != -1 and sum(x == closestV for x in channel['readings']) >= self.eq_readings:
                for i, button in enumerate(channel['buttons']):
                    is_pressed = button in channel['V'][closestV][1]
                    if button['down'] != is_pressed:
                        self.mqtt_broadcast_state(button, is_pressed)
                        button['down'] = is_pressed

            channel['cur_reading'] += 1
            if channel['cur_reading'] >= self.max_readings:
                channel['cur_reading'] = 0

    def read_channel(self, channel):
        return self.rpi.io[channel].value

    def programend(self):
        logging.info("stopping")

        for button in self.buttons.values():
            self.mqtt_broadcast_button_availability(button, "offline")

        self.mqttclient.disconnect()
        self.rpi.exit()
        time.sleep(0.5)
        logging.info("stopped")

    def mqtt_on_connect(self, client, userdata, flags, rc):
        logging.info("MQTT client connected with result code "+str(rc))

        #Configure MQTT for buttons
        for button in self.buttons.values():
            self.configure_mqtt_for_button(button)

        #Broadcast current button state to MQTT for buttons
        for button in self.buttons.values():
            self.mqtt_broadcast_button_availability(button, "online")

    def mqtt_broadcast_button_availability(self, button, value):
       logging.debug("Broadcasting MQTT message on topic: " + button["mqtt_availability_topic"] + ", value: " + value)
       self.mqttclient.publish(button["mqtt_availability_topic"], payload=value, qos=0, retain=True)

    def mqtt_broadcast_state(self, button, is_pressed):
        if is_pressed:
            mqtt_payload = "down"
        else:
            mqtt_payload = "up"
        logging.debug("Broadcasting MQTT message on topic: " + button["mqtt_state_topic"] + ", value: " + mqtt_payload)
        self.mqttclient.publish(button["mqtt_state_topic"], payload=mqtt_payload, qos=0, retain=False)

if __name__ == "__main__":
    mqttLightControl =  ButtonControl()
    mqttLightControl.start()

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
import datetime
import revpimodio2
import paho.mqtt.client as mqtt
import time
import logging
import atexit
from itertools import chain, combinations

SHORT_PRESS = 'click'
LONG_PRESS = 'hold'

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
    V_acc = 80
    unique_id_suffix = '_radcab'
    long_press = None

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

        for key in ['topic_prefix', 'homeassistant_prefix', 'mqtt_server_ip', 'mqtt_server_port', 'mqtt_server_user', 'mqtt_server_password', 'R0', 'buttons', 'groups', 'unique_id_suffix', 'long_press']:
            try:
                self.__setattr__(key, config[key])
            except KeyError:
                pass

        self.availability_topic = self.topic_prefix + '/bridge/state'

        for id, button in self.buttons.items():
            button['id'] = id

            if not 'name' in button:
                button['name'] = button["id"]

            if not 'long_press' in button:
                button['long_press'] = self.long_press

            if button['long_press'] == -1:
                button['long_press'] = None

            if not 'unique_id' in button:
                button['unique_id'] = button["id"].replace('/', '_')
            button['unique_id'] += self.unique_id_suffix

            for k, v in self.default_button.items():
                if not k in button:
                    button[k] = v

            button["mqtt_state_topic"] = "{}/{}/state".format(self.topic_prefix, button["id"])
            button["mqtt_click_topic"] = "{}/{}/click".format(self.topic_prefix, button["id"])
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

    def configure_mqtt_for_sensor(self, button):
        button_configuration = {
            "unique_id": button["unique_id"],
            "name": button["name"] + "_button",
            "state_topic": button["mqtt_state_topic"],
            "payload_on": "down",
            "payload_off": "up",
            "availability": [
                {'topic': self.availability_topic},
                {'topic': button["mqtt_availability_topic"]},
            ],
            "device": {
                "identifiers": [button["unique_id"]],
                "manufacturer": "KUNBUS GmbH",
                "model": "RevPi Analog Buttons",
                "name": button["name"] + "_button",
                "sw_version": "radcab"
            },
        }

        json_conf = json.dumps(button_configuration)
        logging.debug("Broadcasting homeassistant configuration for button: " + button["name"] + ":" + json_conf)
        config_topic = "{}/binary_sensor/{}/config".format(self.homeassistant_prefix, button["unique_id"])
        self.mqttclient.publish(config_topic, payload=json_conf, qos=0, retain=True)

    def configure_mqtt_for_button(self, button):
        for press_type in [SHORT_PRESS, LONG_PRESS]:
            button_configuration = {
                "automation_type": "trigger",
                "device": {
                    "identifiers": [button["unique_id"]],
                    "manufacturer": "KUNBUS GmbH",
                    "model": "RevPi Analog Buttons",
                    "name": button["name"] + "_button",
                    "sw_version": "radcab"
                },
                "topic": button["mqtt_click_topic"],
                "payload": press_type,
                "type": "click",
                "subtype": press_type,
            }

            json_conf = json.dumps(button_configuration)
            logging.debug("Broadcasting homeassistant configuration for button: " + button["name"] + ":" + json_conf)
            config_topic = "{}/device_automation/{}/{}/config".format(self.homeassistant_prefix, button["unique_id"], press_type)
            self.mqttclient.publish(config_topic, payload=json_conf, qos=0, retain=True)
        
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

            closestV = 0  #if v < 150 else None
            for i in range(len(channel['V'])):
                dist = abs(v-channel['V'][i][0])
                if dist < self.V_acc \
                and (closestV is None or dist < abs(channel['V'][closestV][0]-channel['V'][i][0])):
                    closestV = i

            channel['readings'][channel['cur_reading']] = closestV

            if closestV is not None:
                down_buttons = channel['V'][closestV][1]

                if len(down_buttons) > 0 and logging.getLogger().isEnabledFor(logging.DEBUG):
                    logging.debug('on channel {} read value {}, closest buttons: {}'.format(channel['id'], v, ', '.join(x['id'] for x in down_buttons)))
                
                if sum(x == closestV for x in channel['readings']) >= self.eq_readings:
                    for i, button in enumerate(channel['buttons']):
                        is_pressed = button in down_buttons
                        if (not not button['down']) != is_pressed:
                            self.mqtt_broadcast_state(button, is_pressed)
                            if button['long_press'] is None:
                                if is_pressed:
                                    self.mqtt_broadcast_click(button, SHORT_PRESS)
                            elif not is_pressed and isinstance(button['down'], datetime.datetime):
                                self.mqtt_broadcast_click(button, SHORT_PRESS)

                            button['down'] = datetime.datetime.now() if is_pressed else False
                        elif is_pressed and isinstance(button['down'], datetime.datetime) and button['long_press'] is not None:
                            if (datetime.datetime.now() - button['down']) / datetime.timedelta(milliseconds=1) >= button['long_press']:
                                self.mqtt_broadcast_click(button, LONG_PRESS)
                                button['down'] = True

            channel['cur_reading'] += 1
            if channel['cur_reading'] >= self.max_readings:
                channel['cur_reading'] = 0

    def read_channel(self, channel):
        return self.rpi.io[channel].value

    def programend(self):
        logging.info("stopping")

        self.mqttclient.publish(self.availability_topic, payload="offline", qos=0, retain=True)

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
            self.configure_mqtt_for_sensor(button)

        #Broadcast current button state to MQTT for buttons
        for button in self.buttons.values():
            self.mqtt_broadcast_button_availability(button, "online")

        self.mqttclient.publish(self.availability_topic, payload="online", qos=0, retain=True)

    def mqtt_broadcast_button_availability(self, button, value):
       logging.debug("Broadcasting MQTT message on topic: " + button["mqtt_availability_topic"] + ", value: " + value)
       self.mqttclient.publish(button["mqtt_availability_topic"], payload=value, qos=0, retain=True)

    def mqtt_broadcast_state(self, button, is_pressed):
        #pressed_time = datetime.timedelta(total_seconds=0) if is_pressed else datetime.datetime.now() - button['down']
        #
        #mqtt_payload = {
        #    'event': 'down' if is_pressed else 'up',
        #    'pressed_time': pressed_time.total_seconds * 1000 + pressed_time.milliseconds
        #}
        #
        #json_payload = json.dumps(mqtt_payload)
        json_payload = 'down' if is_pressed else 'up'
        logging.debug("Broadcasting MQTT message on topic: " + button["mqtt_state_topic"] + ", value: " + json_payload)
        self.mqttclient.publish(button["mqtt_state_topic"], payload=json_payload, qos=0, retain=False)


    def mqtt_broadcast_click(self, button, action):
        logging.debug("Broadcasting MQTT message on topic: " + button["mqtt_click_topic"] + ", value: " + action)
        self.mqttclient.publish(button["mqtt_click_topic"], payload=action, qos=0, retain=False)

if __name__ == "__main__":
    mqttLightControl =  ButtonControl()
    mqttLightControl.start()

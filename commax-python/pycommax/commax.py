import paho.mqtt.client as mqtt
import socket
import json
import time
import traceback
import asyncio

share_dir = '/share'
config_dir = '/data'
data_dir = '/pycommax'


def log(string):
    date = time.strftime('%Y-%m-%d %p %I:%M:%S', time.localtime(time.time()+9*60*60))
    print('[{}] {}'.format(date, string))
    return


def find_device(config):
    # 접속 정보 설정
    data_size = 32
    socket_size = int(data_size / 2)
    socket_address = (config['socket_IP'], config['socket_port'])

    # start with a socket at 5-second timeout
    log("Creating the socket to find devices..")
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(5.0)
    kk = 0
    while kk < 1:
        # check and turn on TCP Keepalive
        x = sock.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE)
        if x == 0:
            log('Socket Keepalive off, turning on')
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            x = sock.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE)
            log('setsockopt={}'.format(x))
        else:
            log('Socket Keepalive already on')

        try:
            sock.connect(socket_address)
            kk = 1
        except socket.error:
            log('Socket connect failed! Loop up and try socket again')
            traceback.print_exc()
            time.sleep(5.0)
            kk = 0
            continue

    collect_data = []
    target_time = time.time() + 20
    log('Socket connect worked! Find devices for 20s...')
    while time.time() < target_time:
        try:
            req = sock.recv(socket_size)
        except socket.timeout:
            log('Socket timeout, loop and try recv() again')
            time.sleep(5.0)
            # traceback.print_exc()
            continue
        except:
            traceback.print_exc()
            log('Other Socket err, exit and try creating socket again')
            # break from loop
            break

        data = req.hex().upper()
        if len(data) == data_size:
            collect_data.append(data)

    sock.close()
    collect_data = list(set(collect_data))
    with open(data_dir + '/commax_devinfo.json') as file:
        dev_info = json.load(file)

    collected_list = {}
    for name in dev_info:
        collected_list[name] = sorted(
            list(filter(lambda hex_data: hex_data.startswith(dev_info[name]['statePREFIX']), collect_data)))

    for key in collected_list:
        if key == 'Thermo' or key == 'Light':
            dev_info[key]['Number'] = len(sorted([hex_data[:16] for hex_data in collected_list[key]]))
        elif key == 'LightBreaker' or key == 'Gas':
            if len(collected_list[key]) < 2:
                dev_info[key]['Number'] = len(collected_list[key])
            else:
                dev_info[key]['Number'] = 1
                dev_info[key]['stateOFF'] = collected_list[key][0][16:]
                dev_info[key]['stateON'] = collected_list[key][1][16:]
        elif key == 'Fan' or key == 'EV':
            dev_info[key]['Number'] = 0 if len(collected_list[key]) < 1 else 1

    with open(share_dir + '/commax_found_device.json', 'w', encoding='utf-8') as make_file:
        json.dump(dev_info, make_file, indent="\t")
        log('Writing device_list to : /share/commax_found_device.json')
    return dev_info


def do_work(config, device_list):
    # 변수 지정
    mqtt_log = config['mqtt_log']
    find_signal = config['save_unregistered_signal']
    debug = config['DEBUG']
    check_signal = config['check_all_received_signal']
    data_size = 32
    socket_size = int(data_size / 2)

    STATE_TOPIC = 'homenet/{}/{}/state'

    def pad(value):
        value = int(value)
        return '0' + str(value) if value < 10 else str(value)

    # checksum 계산
    def checksum(input_hex):
        try:
            input_hex = input_hex[:14]
            s1 = sum([int(input_hex[val], 16) for val in range(0, 14, 2)])
            s2 = sum([int(input_hex[val + 1], 16) for val in range(0, 14, 2)])
            s1 = s1 + int(s2 // 16)
            s1 = s1 % 16
            s2 = s2 % 16
            return input_hex + format(s1, 'X') + format(s2, 'X')
        except:
            return None

    def make_hex(k, input_hex, change):
        if input_hex:
            try:
                change = int(change)
                input_hex = '{}{}{}'.format(input_hex[:change - 1], int(input_hex[change - 1]) + k, input_hex[change:])
                return checksum(input_hex)
            except:
                return input_hex
        else:
            return None

    def make_hex_temp(k, curTemp, setTemp, state):  # 온도조절기 16자리 (8byte) hex 만들기
        if state == 'OFF' or state == 'ON' or state == 'CHANGE':
            tmp_hex = device_list['Thermo'].get('command' + state)
            change = device_list['Thermo'].get('commandNUM')
            tmp_hex = make_hex(k, tmp_hex, change)
            if state == 'CHANGE':
                setT = pad(setTemp)
                chaTnum = OPTION['Thermo'].get('chaTemp')
                tmp_hex = tmp_hex[:chaTnum - 1] + setT + tmp_hex[chaTnum + 1:]
            return checksum(tmp_hex)
        else:
            tmp_hex = device_list['Thermo'].get(state)
            change = device_list['Thermo'].get('stateNUM')
            tmp_hex = make_hex(k, tmp_hex, change)
            setT = pad(setTemp)
            curT = pad(curTemp)
            curTnum = OPTION['Thermo'].get('curTemp')
            setTnum = OPTION['Thermo'].get('setTemp')
            tmp_hex = tmp_hex[:setTnum - 1] + setT + tmp_hex[setTnum + 1:]
            tmp_hex = tmp_hex[:curTnum - 1] + curT + tmp_hex[curTnum + 1:]
            if state == 'stateOFF':
                return checksum(tmp_hex)
            elif state == 'stateON':
                tmp_hex2 = tmp_hex[:3] + str(3) + tmp_hex[4:]
                return [checksum(tmp_hex), checksum(tmp_hex2)]
            return None

    def make_device_info(device):
        num = device.get('Number')
        if num > 0:
            prefix = device.get('statePREFIX')
            arr = {k + 1: {cmd + onoff: make_hex(k, device.get(cmd + onoff), device.get(cmd + 'NUM'))
                           for cmd in ['command', 'state'] for onoff in ['ON', 'OFF']} for k in range(num)}
            if prefix == '76':
                tmp_hex = arr[1]['stateON']
                change = device_list['Fan'].get('speedNUM')
                arr[1]['stateON'] = [make_hex(k, tmp_hex, change) for k in range(3)]
                tmp_hex = device_list['Fan'].get('commandCHANGE')
                arr[1]['CHANGE'] = [make_hex(k, tmp_hex, change) for k in range(3)]

            arr['Num'] = num
            arr['prefix'] = prefix
            return arr
        else:
            return None

    DEVICE_LISTS = {}
    for name in device_list:
        device_info = make_device_info(device_list[name])
        if device_info:
            DEVICE_LISTS[name] = device_info
    prefix_list = {DEVICE_LISTS[name]['prefix']: name for name in DEVICE_LISTS}
    log('----------------------')
    log('Registered device lists..')
    log('DEVICE_LISTS: {}'.format(DEVICE_LISTS))
    log('----------------------')

    HOMESTATE = {}
    QUEUE = []
    COLLECTDATA = {'cond': find_signal, 'data': [], 'EVtime': time.time(), 'LastRecv': time.time_ns()}

    def on_connect(client, userdata, flags, rc):
        if rc == 0:
            log("MQTT connection successful!!")
            client.subscribe('homenet/#', 0)
        else:
            errcode = {1: 'Connection refused - incorrect protocol version',
                       2: 'Connection refused - invalid client identifier',
                       3: 'Connection refused - server unavailable',
                       4: 'Connection refused - bad username or password',
                       5: 'Connection refused - not authorised'}
            log(errcode[rc])

    def on_message(client, userdata, msg):
        topics = msg.topic.split('/')
        if topics[-1] == 'command':
            key = topics[1] + topics[2]
            device = topics[1][:-1]
            idx = int(topics[1][-1])
            value = msg.payload.decode('utf-8')
            if mqtt_log:
                log('[LOG] HA >> MQTT : {} -> {}'.format(msg.topic, value))
            try:
                if device in DEVICE_LISTS:
                    if HOMESTATE.get(key) and value != HOMESTATE.get(key):
                        if device == 'Thermo':
                            curTemp = HOMESTATE.get(topics[1]+'curTemp')
                            setTemp = HOMESTATE.get(topics[1]+'setTemp')
                            if value == 'off':
                                value = 'OFF'
                            elif value == 'heat':
                                value = 'ON'
                            if topics[2] == 'power':
                                sendcmd = make_hex_temp(idx-1, curTemp, setTemp, value)
                                recvcmd = make_hex_temp(idx-1, curTemp, setTemp, 'state'+value)
                                if sendcmd:
                                    QUEUE.append({'sendcmd': sendcmd, 'recvcmd': recvcmd, 'count': 0})
                                    if debug:
                                        log('[DEBUG] Queued ::: sendcmd: {}, recvcmd: {}'.format(sendcmd, recvcmd))
                            elif topics[2] == 'setTemp':
                                value = int(float(value))
                                if value == int(setTemp):
                                    if debug:
                                        log('[DEBUG] {} is already set: {}'.format(topics[1], value))
                                else:
                                    setTemp = value
                                    sendcmd = make_hex_temp(idx-1, curTemp, setTemp, 'CHANGE')
                                    recvcmd = make_hex_temp(idx-1, curTemp, setTemp, 'stateON')
                                    if sendcmd:
                                        QUEUE.append({'sendcmd': sendcmd, 'recvcmd': recvcmd, 'count': 0})
                                        if debug:
                                            log('[DEBUG] Queued ::: sendcmd: {}, recvcmd: {}'.format(sendcmd, recvcmd))
                        elif device == 'Fan':
                            if value == 'off':
                                value = 'OFF'
                            if topics[2] == 'power':
                                sendcmd = DEVICE_LISTS[device][idx].get('command' + value)
                                recvcmd = DEVICE_LISTS[device][idx].get('state' + value) if value == 'ON' else [DEVICE_LISTS[device][idx].get('state' + value)]
                                QUEUE.append({'sendcmd': sendcmd, 'recvcmd': recvcmd, 'count': 0})
                                if debug:
                                    log('[DEBUG] Queued ::: sendcmd: {}, recvcmd: {}'.format(sendcmd, recvcmd))
                            elif topics[2] == 'speed':
                                speed_list = ['low', 'medium', 'high']
                                if value in speed_list:
                                    index = speed_list.index(value)
                                    sendcmd = DEVICE_LISTS[device][idx]['CHANGE'][index]
                                    recvcmd = [DEVICE_LISTS[device][idx]['stateON'][index]]
                                    QUEUE.append({'sendcmd': sendcmd, 'recvcmd': recvcmd, 'count': 0})
                                    if debug:
                                        log('[DEBUG] Queued ::: sendcmd: {}, recvcmd: {}'.format(sendcmd, recvcmd))
                        else:
                            sendcmd = DEVICE_LISTS[device][idx].get('command' + value)
                            if sendcmd:
                                recvcmd = [DEVICE_LISTS[device][idx].get('state' + value, 'NULL')]
                                QUEUE.append({'sendcmd': sendcmd, 'recvcmd': recvcmd, 'count': 0})
                                if debug:
                                    log('[DEBUG] Queued ::: sendcmd: {}, recvcmd: {}'.format(sendcmd, recvcmd))
                    else:
                        if debug:
                            log('[DEBUG] {} is already set: {}'.format(key, value))
                else:
                    if debug:
                        log('[DEBUG] There is no commands for {}'.format(msg.topic))
            except Exception as err:
                log('[ERROR] mqtt_on_message(): {}'.format(err))

    mqtt_client = mqtt.Client('homenet-commax-python')
    mqtt_client.username_pw_set(config['mqtt_id'], config['mqtt_password'])
    mqtt_client.on_connect = on_connect
    mqtt_client.on_message = on_message
    mqtt_client.connect_async(config['mqtt_server'])
    mqtt_client.loop_start()

    async def update_state(device, idx, onoff):
        state = 'power'
        deviceID = device + str(idx + 1)
        key = deviceID + state

        if onoff != HOMESTATE.get(key):
            HOMESTATE[key] = onoff
            topic = STATE_TOPIC.format(deviceID, state)
            mqtt_client.publish(topic, onoff.encode())
            if mqtt_log:
                log('[LOG] MQTT >> HA : {} >> {}'.format(topic, onoff))
        else:
            if debug:
                log('[DEBUG] {} is already set: {}'.format(deviceID, onoff))
        return

    async def update_fan(device, idx, onoff):
        deviceID = device + str(idx + 1)
        if onoff == 'ON' or onoff == 'OFF':
            state = 'power'
            key = deviceID + state
        else:
            try:
                speed_list = ['low', 'medium', 'high']
                onoff = speed_list[int(onoff)-1]
                state = 'speed'
                key = deviceID + state
            except:
                return
        if onoff != HOMESTATE.get(key):
            HOMESTATE[key] = onoff
            topic = STATE_TOPIC.format(deviceID, state)
            mqtt_client.publish(topic, onoff.encode())
            if mqtt_log:
                log('[LOG] MQTT >> HA : {} >> {}'.format(topic, onoff))
        else:
            if debug:
                log('[DEBUG] {} is already set: {}'.format(deviceID, onoff))
        return

    async def update_temperature(idx, curTemp, setTemp):
        deviceID = 'Thermo' + str(idx + 1)
        temperature = {'curTemp': pad(curTemp), 'setTemp': pad(setTemp)}
        for state in temperature:
            key = deviceID + state
            val = temperature[state]
            if val != HOMESTATE.get(key):
                HOMESTATE[key] = val
                topic = STATE_TOPIC.format(deviceID, state)
                mqtt_client.publish(topic, val.encode())
                if mqtt_log:
                    log('[LOG] MQTT >> HA : {} -> {}'.format(topic, val))
            else:
                if debug:
                    log('[DEBUG] {} is already set: {}'.format(key, val))
        return

    async def recv_from_socket(READER):
        try:
            req = await READER.read(socket_size)
            if HOMESTATE.get('EV1power') == 'ON':
                if COLLECTDATA['EVtime'] < time.time():
                    await update_state('EV', 0, 'OFF')

            data = req.hex().upper()
            if data:
                OutBreak = False
                for que in QUEUE:
                    for recvcmd in que['recvcmd']:
                        if recvcmd in data:
                            QUEUE.remove(que)
                            if debug:
                                log('[DEBUG] Found matched hex: {}. Delete a queue: {}'.format(data, que))
                            OutBreak = True
                            break
                    if OutBreak:
                        break

                if check_signal:
                    log('[SIGNAL] receved: {}'.format(data))
                data_prefix = data[:2]
                if data_prefix in prefix_list:
                    device_name = prefix_list[data_prefix]
                    if len(data) == 32:
                        data = data[16:]
                        if device_name == 'Thermo' and data.startswith(device_list['Thermo']['stateOFF'][:2]):
                            curTnum = device_list['Thermo']['curTemp']
                            setTnum = device_list['Thermo']['setTemp']
                            curT = data[curTnum - 1:curTnum + 1]
                            setT = data[setTnum - 1:setTnum + 1]
                            onoffNUM = device_list['Thermo']['stateONOFFNUM']
                            staNUM = device_list['Thermo']['stateNUM']
                            index = int(data[staNUM - 1]) - 1
                            onoff = 'ON' if int(data[onoffNUM - 1]) > 0 else 'OFF'

                            await update_state(device_name, index, onoff)
                            await update_temperature(index, curT, setT)
                        elif device_name == 'Fan':
                            if data in DEVICE_LISTS['Fan'][1]['stateON']:
                                await update_state('Fan', 0, 'ON')
                                speed = DEVICE_LISTS['Fan'][1]['stateON'].index(data)
                                await update_fan('Fan', 0, speed)
                        else:
                            num = DEVICE_LISTS[device_name]['Num']
                            state = [DEVICE_LISTS[device_name][k+1]['stateOFF'] for k in range(num)] + [DEVICE_LISTS[device_name][k+1]['stateON'] for k in range(num)]
                            if data in state:
                                index = state.index(data)
                                onoff, index = ['OFF', index] if index < num else ['ON', index - num]
                                await update_state(device_name, index, onoff)
                    else:
                        if device_name == 'EV':
                            await update_state('EV', 0, 'ON')
                            COLLECTDATA['EVtime'] = time.time() + 3
                else:
                    if COLLECTDATA['cond']:
                        if len(COLLECTDATA['data']) < 20:
                            if data not in COLLECTDATA['data']:
                                log('[FOUND] signal: {}'.format(data))
                                COLLECTDATA['data'].append(data)
                                COLLECTDATA['data'] = list(set(COLLECTDATA['data']))
                        else:
                            COLLECTDATA['cond'] = False
                            with open(share_dir + '/collected_signal.txt', 'w', encoding='utf-8') as make_file:
                                json.dump(COLLECTDATA['data'], make_file, indent="\t")
                                log('[Complete] Collect 20 signals. See : /share/collected_signal.txt')
                            COLLECTDATA['data'] = None
        except Exception as err:
            log('[ERROR] recv_from_socket: {}'.format(err))
            return True
        return

    async def send_to_socket(WRITER):
        try:
            if QUEUE:
                send_data = QUEUE.pop(0)
                if debug:
                    log('[DEBUG] socket:: Send a signal: {}'.format(send_data))
                WRITER.write(bytes.fromhex(send_data['sendcmd']))
                await WRITER.drain()
                await asyncio.sleep(0.4)
                if send_data['count'] < 3:
                    send_data['count'] = send_data['count'] + 1
                    QUEUE.append(send_data)
                else:
                    if debug:
                        log('[ERROR] socket:: Send over 3 times. Send Failure. Delete a queue: {}'.format(send_data))
        except Exception as err:
            log('[ERROR] send_to_socket(): {}'.format(err))
            return True
        await asyncio.sleep(0.1)
        return

    async def socket_process():
        if 'EV' in DEVICE_LISTS:
            await update_state('EV', 0, 'OFF')

        while True:
            reader, writer = await asyncio.open_connection(config['socket_IP'], config['socket_port'])
            for _ in range(10):
                try:
                    err_recv = await recv_from_socket(reader)
                    err_send = await send_to_socket(writer)
                    if err_recv or err_send:
                        writer.close()
                        await writer.wait_closed()
                        break
                except Exception as err:
                    log('[ERROR] send_to_socket(): {}'.format(err))
                    writer.close()
                    await writer.wait_closed()
                    log('Try to reconnect..')
                    break
            writer.close()
            await writer.wait_closed()

    loop = asyncio.get_event_loop()
    # cors = asyncio.wait([send_to_socket(), recv_from_socket()])
    cors = asyncio.wait([socket_process()])
    loop.run_until_complete(cors)
    loop.close()


if __name__ == '__main__':
    with open(config_dir + '/options.json') as file:
        CONFIG = json.load(file)
    try:
        with open(share_dir + '/commax_found_device.json') as file:
            log('Found device data: /share/commax_found_device.json')
            OPTION = json.load(file)
    except IOError:
        OPTION = find_device(CONFIG)

    do_work(CONFIG, OPTION)

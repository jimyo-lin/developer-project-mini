#!/usr/bin/env python3

"""
Example CLI utility to control a Gen 3 sous vide cooker via BLE,
including both:
 - Subcommands for one-off operations (scan, connect, set-temp, etc.)
 - An 'interactive' REPL that keeps the cooker connected until you type 'exit'.
 - A new live temperature chart feature ("plot-temp").

Note: When prompted, please accept pairing on your host computer.
Otherwise, the mini will disconnect.

Now updated so that when you exit the interactive mode, the cooker is always disconnected.
Also, if no subcommand is provided the script will auto-scan and connect in interactive mode,
and it will send the SET CLOCK command immediately upon connection.
"""

import argparse
import asyncio
import datetime
import json
import base64
import logging
from bleak import BleakScanner, BleakClient, BLEDevice, AdvertisementData

# Setup logger
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ----------------------------------------------------------------------------
# BLE Constants and Helper Functions
# ----------------------------------------------------------------------------

GEN_3_SERVICE_UUID = "910772a8-a5e7-49a7-bc6d-701e9a783a5c"
GEN_3_CHARACTERISTICS = {
    "SET_TEMPERATURE": "0f5639f7-3c4e-47d0-9496-0672c89ea48a",
    "CURRENT_TEMPERATURE": "6ffdca46-d6a8-4fb2-8fd9-c6330f1939e3",
    "TIMER": "a2b179f8-944e-436f-a246-c66caaf7061f",
    "STATE": "54e53c60-367a-4783-a5c1-b1770c54142b",
    "SET_CLOCK": "d8a89692-cae8-4b74-96e3-0b99d3637793",
    "SYSTEM_INFO": "153c9432-7c83-4b88-9252-7588229d5473",
}

def encode_command_for_btle(command: dict) -> bytes:
    json_str = json.dumps(command)
    return base64.b64encode(json_str.encode("utf-8"))

def decode_data_from_btle(data: bytes) -> dict:
    try:
        decoded_str = base64.b64decode(data).decode("utf-8")
        return json.loads(decoded_str)
    except Exception as e:
        logger.error("Error decoding data from BTLE: %s", e)
        return {}

class BleMini:
    _instance = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self, parserArgs):
        self.parserArgs = parserArgs
        self.device: BLEDevice | None = None
        self.advData: AdvertisementData | None = None
        self.device_found_event = asyncio.Event()

    def scanCallback(self, device: BLEDevice, adv_data: AdvertisementData):
        
        cooker_id=self.parserArgs.cooker_id
        if(self.parserArgs.command == "scan"):
            logger.info("Found: %s [%s] RSSI=%s", adv_data.local_name, device.address, adv_data.rssi)

        # If the device name doesnt match what the user put in, return immidiately
        if cooker_id and (not adv_data.local_name or not adv_data.local_name.startswith(cooker_id)):
            return
        # The real validation
        if any(uuid.lower() == GEN_3_SERVICE_UUID.lower() for uuid in adv_data.service_uuids):
            logger.info("Found It: %s [%s] RSSI=%s", adv_data.local_name, device.address, adv_data.rssi)
            self.device = device
            self.advData = adv_data
            self.device_found_event.set()
        else:
            return
        
    async def connect(self):
        await run_command2(self)

# ----------------------------------------------------------------------------
# PrecisionCooker: Scanning & Connecting
# ----------------------------------------------------------------------------

# Scan, connect, disconnect are all nice for separating out those specific actions, but bleak kinda handles it all with its heavy use of context managers.
# PrecisionCooker as a class itself only has the member variable connected_device anyways which is handled by the context manager
# Effectively, lets remove all of this 

class PrecisionCooker:
    """
    Responsible for scanning for the device, connecting, and disconnecting.
    """

    def __init__(self):
        self.connected_device = None  # Will be a BleakClient when connected

    # No longer used
    # async def scan_for_btle_device(self, cooker_id: str = None, timeout: float = 10.0) -> BleakClient:
    #     logger.info("Scanning for devices (timeout=%ss, filter=%s)...", timeout, cooker_id or "None")
    #     devices = await BleakScanner.discover(timeout=timeout)
    #     for d in devices:
    #         if cooker_id and (not d.name or not d.name.startswith(cooker_id)):
    #             continue
    #         uuids = d.metadata.get("uuids", [])
    #         if any(uuid.lower() == GEN_3_SERVICE_UUID.lower() for uuid in uuids):
    #             logger.info("Found device: %s [%s]", d.name, d.address)
    #             return BleakClient(d)
    #     # If no device is found, provide additional instructions
    #     raise RuntimeError(
    #         "No suitable device found. If your mini is on and the script is unable to find it, "
    #         "press and hold the top button for 10 seconds until the light turns off. This resets the mini "
    #         "and allows it to pair again."
    #     )

    # # Can Still Be used, just needs a BleakClient, But contextManager instead
    # async def connect(self, client: BleakClient):
    #     logger.info("Connecting to device...")
    #     await client.connect()
    #     self.connected_device = client
    #     logger.info("Connected.")

    #     # Ensure required characteristics exist
    #     await client.get_services()
    #     service = client.services.get_service(GEN_3_SERVICE_UUID)
    #     if service is None:
    #         raise RuntimeError(f"Service {GEN_3_SERVICE_UUID} not found!")
    #     found_char_uuids = [c.uuid.lower() for c in service.characteristics]
    #     for name, uuid in GEN_3_CHARACTERISTICS.items():
    #         if uuid.lower() not in found_char_uuids:
    #             raise RuntimeError(f"Characteristic {name} ({uuid}) not found!")
    #     logger.info("All required characteristics verified.")

    # # No longer needed
    # async def disconnect(self):
    #     if self.connected_device:
    #         if self.connected_device.is_connected:
    #             logger.info("Disconnecting...")
    #             await self.connected_device.disconnect()
    #         self.connected_device = None
    #         logger.info("Disconnected.")

# ----------------------------------------------------------------------------
# PrecisionCookerCommands: High-Level Operations
# ----------------------------------------------------------------------------
# All the commands here just use the underlying BleakClient, PrecisionCooker is not necessary, but we can still use it anyways

class PrecisionCookerCommands:
    """
    Provides async methods to control and read from the sous vide device,
    including merging 'state', 'current temperature', and 'timer' into one dict.
    """
    # data returned from read_gatt_char after decode_data_from_btle is {string : string}

    def __init__(self, cooker: PrecisionCooker):
        self.cooker = cooker

    async def get_system_info(self) -> dict:
        data = await self.cooker.connected_device.read_gatt_char(
            GEN_3_CHARACTERISTICS["SYSTEM_INFO"]
        )
        info = decode_data_from_btle(data)
        logger.info("System Info: %s", info)
        return info

    async def get_state(self) -> dict:
        data = await self.cooker.connected_device.read_gatt_char(
            GEN_3_CHARACTERISTICS["STATE"]
        )
        return decode_data_from_btle(data)

    async def get_current_temperature(self) -> dict:
        data = await self.cooker.connected_device.read_gatt_char(
            GEN_3_CHARACTERISTICS["CURRENT_TEMPERATURE"]
        )
        return decode_data_from_btle(data)

    async def get_timer(self) -> dict:
        data = await self.cooker.connected_device.read_gatt_char(
            GEN_3_CHARACTERISTICS["TIMER"]
        )
        return decode_data_from_btle(data)

    async def get_full_state(self) -> dict:
        """
        Reads:
          - STATE
          - CURRENT_TEMPERATURE
          - TIMER
        Merges them into a single dict with extra fields 
        'currentTemperature' and 'timer'.
        """
        # the old one ran the above, this one should grab every characteristic
        state_data = {}
        for characteristic, charUUID in GEN_3_CHARACTERISTICS.items():
            logger.info("Read Characteristic for: %s", characteristic)
            data = await self.cooker.connected_device.read_gatt_char(charUUID)
            curDecodedData = decode_data_from_btle(data)
            state_data[characteristic] = curDecodedData
            #curDecodedData is a json

        # OLD STUFF
        # state_data = await self.get_state()
        # temp_data = await self.get_current_temperature()
        # timer_data = await self.get_timer()

        # Insert the "current" temperature from temp_data
        # current_temp_val = temp_data.get("current", 0)
        # state_data["currentTemperature"] = current_temp_val

        # # Insert the entire timer structure
        # state_data["timer"] = timer_data

        return state_data

    async def set_clock(self):
        # Remove microseconds to shorten the payload
        now_utc_str = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()
        cmd = {"currentTime": now_utc_str}
        data = encode_command_for_btle(cmd)
        # Use response=True so the OS waits for an acknowledgment
        await self.cooker.connected_device.write_gatt_char(
            GEN_3_CHARACTERISTICS["SET_CLOCK"], data, response=True
        )
        logger.info("Clock set to %s (UTC).", now_utc_str)

    async def set_unit(self, unit: str):
        cmd = {"command": "changeUnit", "payload": {"temperatureUnit": unit}}
        data = encode_command_for_btle(cmd)
        await self.cooker.connected_device.write_gatt_char(
            GEN_3_CHARACTERISTICS["STATE"], data, response=False
        )
        logger.info("Temperature unit changed to %s.", unit)

    async def set_temperature(self, value: float):
        cmd = {"setpoint": value}
        data = encode_command_for_btle(cmd)
        await self.cooker.connected_device.write_gatt_char(
            GEN_3_CHARACTERISTICS["SET_TEMPERATURE"], data, response=False
        )
        logger.info("Set temperature command sent to %s (device's current unit).", value)

    async def start_cook(self, setpoint: float, timer_sec: int = 0, cookable_id="recipe123", cookable_type="recipe"):
        cmd = {
            "command": "start",
            "payload": {
                "setpoint": setpoint,
                "timer": timer_sec,
                "cookableId": cookable_id,
                "cookableType": cookable_type,
            },
        }
        data = encode_command_for_btle(cmd)
        await self.cooker.connected_device.write_gatt_char(
            GEN_3_CHARACTERISTICS["STATE"], data, response=False
        )
        logger.info("Started cook at %s (unit) for %s seconds.", setpoint, timer_sec)

    async def stop_cook(self):
        cmd = {"command": "stop"}
        data = encode_command_for_btle(cmd)
        await self.cooker.connected_device.write_gatt_char(
            GEN_3_CHARACTERISTICS["STATE"], data, response=False
        )
        logger.info("Stop cook command sent.")

    async def get_services(self):
        services = self.cooker.connected_device.services
        services_data = {}
        services_data["characteristics"] = services.characteristics
        services_data["descriptors"] = services.descriptors
        services_data["services"] = services.services
        return services_data


# ----------------------------------------------------------------------------
# Live Temperature Plot Function
# ----------------------------------------------------------------------------

async def plot_temperature(commands: PrecisionCookerCommands):
    """
    Continuously reads the current temperature from the device and updates a live chart.
    The temperature is converted to Fahrenheit if the device's unit is set to 'F'.
    The chart displays the appropriate unit in the Y-axis label and legend,
    and it updates every 2 seconds.
    Close the plot window to exit the loop.
    """
    import matplotlib.pyplot as plt

    plt.ion()  # Turn on interactive mode
    fig, ax = plt.subplots()
    x_data = []
    y_data = []
    temp_label = "Temperature (C)"
    line, = ax.plot(x_data, y_data, '-o', label=temp_label)
    ax.set_xlabel("Time (s)")
    ax.set_ylabel(temp_label)
    ax.set_title("Live Temperature Chart")
    ax.legend()

    start_time = datetime.datetime.now()
    logger.info("Live temperature plotting started. Close the plot window to exit.")

    try:
        while plt.fignum_exists(fig.number):
            # Fetch current device state to determine the temperature unit.
            state = await commands.get_state()
            # Expecting the state to include a key 'temperatureUnit'; default to "C" if not found.
            unit = state.get("temperatureUnit", "C").upper()

            # Read current temperature from device.
            temp_data = await commands.get_current_temperature()
            current_temp = temp_data.get("current", None)
            if current_temp is not None:
                # Convert the temperature to Fahrenheit if needed.
                if unit == "F":
                    converted_temp = current_temp * 9 / 5 + 32
                else:
                    converted_temp = current_temp

                elapsed = (datetime.datetime.now() - start_time).total_seconds()
                x_data.append(elapsed)
                y_data.append(converted_temp)
                # Update line data.
                line.set_data(x_data, y_data)
                # Update y-axis label and legend with the correct unit.
                temp_label = f"Temperature ({unit})"
                ax.set_ylabel(temp_label)
                line.set_label(temp_label)
                ax.legend()
                ax.relim()
                ax.autoscale_view()
                plt.draw()
            await asyncio.sleep(2)  # update every 2 seconds
            plt.pause(0.001)  # allow GUI event processing
    except asyncio.CancelledError:
        pass

# ----------------------------------------------------------------------------
# Interactive REPL Subcommand
# ----------------------------------------------------------------------------

# 

# async def interactive_repl(cooker_id=None, timeout=10.0):
#     """
#     Connect once, then read commands in a loop until 'exit'.
#     This approach keeps the cooker connected for multiple commands.
#     We ensure we always disconnect in a 'finally' block.
#     """
#     cooker = PrecisionCooker()
#     try:
#         # Auto-scan and connect to the first detected mini.
#         device_client = await cooker.scan_for_btle_device(cooker_id=cooker_id, timeout=timeout)
#         await cooker.connect(device_client)
#     except Exception as e:
#         logger.error("Error scanning/connecting in REPL: %s", e)
#         return

#     commands = PrecisionCookerCommands(cooker)
#     # Automatically send the SET CLOCK command upon connection.
#     await commands.set_clock()

#     # Print help text as if the user typed "help"
#     help_text = (
#         "Available commands:\n"
#         "  get-state                => Show device state (+temp +timer)\n"
#         "  set-temp <value>         => Set temperature (in device's current unit)\n"
#         "  set-unit <C|F>           => Change temperature unit\n"
#         "  start-cook <setpoint> [timer_seconds] (default timer: 0 seconds)\n"
#         "  stop-cook                => Stop cooking\n"
#         "  set-clock                => Set device clock to UTC\n"
#         "  get-system-info          => Read system info\n"
#         "  plot-temp                => Display live temperature chart\n"
#         "  exit                     => Disconnect and quit\n"
#     )
#     logger.info("\n%s", help_text)

#     try:
#         while True:
#             try:
#                 line = input(">> ").strip()
#             except (EOFError, KeyboardInterrupt):
#                 line = "exit"

#             if not line:
#                 continue

#             parts = line.split()
#             cmd = parts[0].lower()

#             if cmd == "help":
#                 logger.info("%s", help_text)
#                 continue

#             try:
#                 if cmd == "exit":
#                     logger.info("Exiting interactive mode...")
#                     if cooker.connected_device and cooker.connected_device.is_connected:
#                             try:
#                                 await commands.stop_cook()
#                             except Exception as stop_err:
#                                 logger.error("Error executing stop-cook: %s", stop_err)
#                     break
#                 if cmd == "get-state":
#                     full_state = await commands.get_full_state()
#                     logger.info("State (plus current temp & timer): %s", full_state)
#                 elif cmd == "set-temp":
#                     if len(parts) < 2:
#                         logger.info("Usage: set-temp <value>")
#                         continue
#                     val = float(parts[1])
#                     await commands.set_temperature(val)
#                 elif cmd == "set-unit":
#                     if len(parts) < 2 or parts[1].upper() not in ("C", "F"):
#                         logger.info("Usage: set-unit <C|F>")
#                         continue
#                     await commands.set_unit(parts[1].upper())
#                 elif cmd == "start-cook":
#                     if len(parts) < 2:
#                         logger.info("Usage: start-cook <setpoint> [timer_seconds]")
#                         continue
#                     setp = float(parts[1])
#                     tm = int(parts[2]) if len(parts) >= 3 else 0
#                     await commands.start_cook(setp, tm)
#                 elif cmd == "stop-cook":
#                     await commands.stop_cook()
#                 elif cmd == "set-clock":
#                     await commands.set_clock()
#                 elif cmd == "get-system-info":
#                     await commands.get_system_info()
#                 elif cmd == "plot-temp":
#                     logger.info("Launching live temperature plot. Close the chart window to return to the REPL.")
#                     await plot_temperature(commands)
#                 else:
#                     logger.info("Unknown command: %s. Type 'help' for usage.", cmd)
#             except Exception as sub_e:
#                 logger.error("Error executing command: %s", sub_e)
#     finally:
#         await cooker.disconnect()

# Need a new one
async def interactive_repl2(cmds):
    # Automatically send the SET CLOCK command upon connection.
    await cmds.set_clock()

    # Print help text as if the user typed "help"
    help_text = (
        "Available commands:\n"
        "  get-state                => Show device state (+temp +timer)\n"
        "  set-temp <value>         => Set temperature (in device's current unit)\n"
        "  set-unit <C|F>           => Change temperature unit\n"
        "  start-cook <setpoint> [timer_seconds] (default timer: 0 seconds)\n"
        "  stop-cook                => Stop cooking\n"
        "  set-clock                => Set device clock to UTC\n"
        "  get-system-info          => Read system info\n"
        "  plot-temp                => Display live temperature chart\n"
        "  get-services             => Display chracteristics, descriptors, and services\n"
        "  exit                     => Disconnect and quit\n"
    )

    logger.info("\n%s", help_text)
    running = True
    while running:
        try:
            line = input(">> ").strip()
        except (EOFError, KeyboardInterrupt):
            line = "exit"

        if not line:
            continue
        parts = line.split()
        interactiveCmd = parts[0].lower()

        try:
            match interactiveCmd:
                case "help":
                    logger.info("%s", help_text)
                case "exit":
                    logger.info("Exiting interactive mode...")
                    try:
                        await cmds.stop_cook()
                    except Exception as stop_err:
                        logger.error("Error executing stop-cook: %s", stop_err)
                    running = False
                case "get-state":
                    full_state = await cmds.get_full_state()
                    logger.info("State (plus current temp & timer): %s", full_state)
                case "set-temp":
                    if len(parts) < 2:
                        logger.info("Usage: set-temp <value>")
                        continue
                    val = float(parts[1])
                    await cmds.set_temperature(val)
                case "set-unit":
                    if len(parts) < 2 or parts[1].upper() not in ("C", "F"):
                        logger.info("Usage: set-unit <C|F>")
                        continue
                    await cmds.set_unit(parts[1].upper())
                case "start-cook":
                    if len(parts) < 2:
                        logger.info("Usage: start-cook <setpoint> [timer_seconds]")
                        continue
                    setp = float(parts[1])
                    tm = int(parts[2]) if len(parts) >= 3 else 0
                    await cmds.start_cook(setp, tm)
                case "stop-cook":
                    await cmds.stop_cook()
                case "set-clock":
                    await cmds.set_clock()
                case "get-system-info":
                    await cmds.get_system_info()
                case "plot-temp":
                    logger.info("Launching live temperature plot. Close the chart window to return to the REPL.")
                    await plot_temperature(cmds)
                case "get-services":
                    serviceInfo = await cmds.get_services()
                    for dataName, dataData in serviceInfo.items():
                        for charId, curObject in dataData.items():
                            logger.info("%s %d: %s", dataName, charId, curObject.description)

                case _:
                    logger.info("Unknown command: %s. Type 'help' for usage.", cmd)
        except Exception as sub_e:
            logger.error("Error executing command: %s", sub_e)



# ----------------------------------------------------------------------------
# Argparse Setup
# ----------------------------------------------------------------------------

def build_arg_parser():
    parser = argparse.ArgumentParser(
        description="Control the Gen 3 sous vide cooker via BLE.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )

    subparsers = parser.add_subparsers(dest="command", help="Sub-commands (choose one)")

    # scan
    scan_parser = subparsers.add_parser("scan", help="Scan for sous vide cookers.")
    scan_parser.add_argument("--cooker-id", default=None, help="Optional name prefix filter.")
    scan_parser.add_argument("--timeout", type=float, default=10.0, help="Scan timeout in seconds.")

    # connect
    connect_parser = subparsers.add_parser("connect", help="Connect to a device, then disconnect.")
    connect_parser.add_argument("--cooker-id", default=None, help="Optional name prefix filter.")
    connect_parser.add_argument("--timeout", type=float, default=10.0, help="Scan timeout.")

    # set-temp
    st_parser = subparsers.add_parser("set-temp", help="Set target temperature in device's current unit (C or F).")
    st_parser.add_argument("temperature", type=float, help="Temperature to set.")

    # set-unit
    su_parser = subparsers.add_parser("set-unit", help="Change device temperature unit to C or F.")
    su_parser.add_argument("unit", choices=["C", "F"], help="Desired temperature unit.")

    # start-cook
    sc_parser = subparsers.add_parser("start-cook", help="Start cooking at a setpoint with an optional timer (in seconds).")
    sc_parser.add_argument("--setpoint", type=float, required=True, help="Temperature setpoint (device's unit).")
    sc_parser.add_argument("--timer", type=int, default=0, required=False, help="Timer in seconds (default: 0).")
    sc_parser.add_argument("--cookable-id", default="recipe123", help="Cookable ID (optional).")
    sc_parser.add_argument("--cookable-type", default="recipe", help="Cookable type (optional).")

    # stop-cook
    subparsers.add_parser("stop-cook", help="Stop current cook.")

    # set-clock
    subparsers.add_parser("set-clock", help="Set device clock to current UTC.")

    # get-state
    subparsers.add_parser("get-state", help="Read device state, current temperature, and timer in one dict.")

    # disconnect
    subparsers.add_parser("disconnect", help="Disconnect (no-op in one-off mode).")

    # interactive
    interactive_parser = subparsers.add_parser("interactive", help="Launch a REPL, staying connected until 'exit'.")
    interactive_parser.add_argument("--cooker-id", default=None, help="Optional name prefix filter.")
    interactive_parser.add_argument("--timeout", type=float, default=10.0, help="Scan timeout.")

    # plot-temp (live temperature chart)
    subparsers.add_parser("plot-temp", help="Display a live temperature chart from the device.")

    return parser

# ----------------------------------------------------------------------------
# Main Command Dispatcher
# ----------------------------------------------------------------------------

# The main Command Dispatcher uses connect disconnect. Using connect disconnect can be somewhat dangerous as you can leave a device connected if not careful
# It is strongly suggested to use context manager by creating a BleakClient w/ a BLEDevice from the BleakScanner.

# Current way ive implemented is no good, i need some sort of class or global to handle found devices. Not gonna lie, this is a bit messy from Bleak

# Nearly all the commands will run a cooker.scan_for_btle_device which runs BleakScanner.discover
# So all commands run BleakScanner.discover. Well, lets do that and with passive scanning then

# Rather instead of using the BleakScanner.discover class method lets create a a BleakScanner object, lets do that below
# hmmm, need the arguments too

async def run_command2(bleMini:BleMini):
    device = bleMini.device
    command = bleMini.parserArgs.command
    args = bleMini.parserArgs

    #hmmm, all these commands require connecting anyways
    try:
        async with BleakClient(device, pair = True) as client:
            cooker = PrecisionCooker()
            cooker.connected_device = client
            cmds = PrecisionCookerCommands(cooker)

            match command:
                case "connect" | "set-clock":
                    await cmds.set_clock()
                    logger.info("Connected successfully!")
                case "set-temp":
                    await cmds.set_temperature(args.temperature)
                case "set-unit":
                    await cmds.set_unit(args.unit)
                case "start-cook":
                    await cmds.start_cook(
                        setpoint=args.setpoint,
                        timer_sec=args.timer,
                        cookable_id=args.cooker_id if hasattr(args, "cooker_id") else "recipe123",
                        cookable_type=args.cookable_type if hasattr(args, "cookable_type") else "recipe"
                    )
                case "stop-cook":
                    await cmds.stop_cook()
                case "get-state":
                    full_state = await cmds.get_full_state()  # merges state+temp+timer
                    logger.info("Current device state (incl. temp & timer): %s", full_state)
                case "plot-temp":
                    logger.info("Starting live temperature plot. Close the chart window to exit.")
                    await plot_temperature(cmds)
                case "interactive":
                    await interactive_repl2(cmds)
                case _:
                    logger.info("Unknown command: %s", command)


    except BleakDBusError as e:
        print(f"[Linux/BlueZ Error] Pairing failed via D-Bus: {e.dbus_error}")
        print("Check if your PIN was correct or if an authentication agent is running.")
    
    except BleakError as e:
        print(f"[Bleak Error] General BLE framework failure: {e}")
        
    except asyncio.TimeoutError:
        print("[Timeout Error] The pairing request timed out. Did the user respond to the PIN prompt?")
    except Exception as e:
        logger.error("Error: %s", e)

# just not going to use this anymore
#async def run_command(command, args):
# async def run_command(command, args, device, adv_data):
#     """
#     Dispatch function for each subcommand. By default, each command connects->runs->disconnects.
#     The 'interactive' subcommand keeps the cooker connected for multiple commands in a loop.
#     """

#     if command == "interactive":
#         await interactive_repl(cooker_id=args.cooker_id, timeout=args.timeout)
#         return

#     cooker = PrecisionCooker()
#     cmds = PrecisionCookerCommands(cooker)

#     if command == "scan":
#         # try:
#         #     logger.info("Scanning for devices...")
#         #     devices = await BleakScanner.discover(timeout=args.timeout)
#         #     for d in devices:
#         #         logger.info("Found: %s [%s] RSSI=%s", d.name, d.address, d.rssi)
#         # except Exception as e:
#         #     logger.error("Error scanning: %s", e)
#         # This is already handled by pre_run_command
#         pass
        
#     elif command == "connect":
#         try:
#             #client = await cooker.scan_for_btle_device(cooker_id=args.cooker_id, timeout=args.timeout)
#             #await cooker.connect(client)
#             # Instead of cooker.connect(client) we're going to connect using a context manager
#             async with BleakClient(device, pair = True) as client:
#                 await cmds.set_clock()
#                 logger.info("Connected successfully!")

#             #await cooker.disconnect()

#         except Exception as e:
#             logger.error("Error connecting: %s", e)
#     elif command == "set-temp":
#         try:
#             client = await cooker.scan_for_btle_device(timeout=10)
#             await cooker.connect(client)
#             await cmds.set_temperature(args.temperature)
#             await cooker.disconnect()
#         except Exception as e:
#             logger.error("Error setting temperature: %s", e)
#     elif command == "set-unit":
#         try:
#             client = await cooker.scan_for_btle_device(timeout=10)
#             await cooker.connect(client)
#             await cmds.set_unit(args.unit)
#             await cooker.disconnect()
#         except Exception as e:
#             logger.error("Error setting temperature unit: %s", e)
#     elif command == "start-cook":
#         try:
#             client = await cooker.scan_for_btle_device(timeout=10)
#             await cooker.connect(client)
#             await cmds.start_cook(
#                 setpoint=args.setpoint,
#                 timer_sec=args.timer,
#                 cookable_id=args.cooker_id if hasattr(args, "cooker_id") else "recipe123",
#                 cookable_type=args.cookable_type if hasattr(args, "cookable_type") else "recipe"
#             )
#             await cooker.disconnect()
#         except Exception as e:
#             logger.error("Error starting cook: %s", e)
#     elif command == "stop-cook":
#         try:
#             client = await cooker.scan_for_btle_device(timeout=10)
#             await cooker.connect(client)
#             await cmds.stop_cook()
#             await cooker.disconnect()
#         except Exception as e:
#             logger.error("Error stopping cook: %s", e)
#     elif command == "set-clock":
#         try:
#             client = await cooker.scan_for_btle_device(timeout=10)
#             await cooker.connect(client)
#             await cmds.set_clock()
#             await cooker.disconnect()
#         except Exception as e:
#             logger.error("Error setting clock: %s", e)
#     elif command == "get-state":
#         try:
#             client = await cooker.scan_for_btle_device(timeout=10)
#             await cooker.connect(client)
#             full_state = await cmds.get_full_state()  # merges state+temp+timer
#             logger.info("Current device state (incl. temp & timer): %s", full_state)
#             await cooker.disconnect()
#         except Exception as e:
#             logger.error("Error reading state: %s", e)
#     elif command == "disconnect":
#         logger.info("No persistent connection to close in subcommand mode.")
#     elif command == "plot-temp":
#         try:
#             client = await cooker.scan_for_btle_device(timeout=10)
#             await cooker.connect(client)
#                 logger.info("Starting live temperature plot. Close the chart window to exit.")
#                 await plot_temperature(cmds)
#         except Exception as e:
#             logger.error("Error plotting temperature: %s", e)
#         finally:
#             await cooker.disconnect()
#     else:
#         logger.info("Unknown command: %s", command)

async def main():
    parser = build_arg_parser()
    args = parser.parse_args()

    # If no subcommand is provided, default to interactive mode.
    if not args.command:
        args.command = "interactive"
        args.cooker_id = None
        args.timeout = 10.0

    # Create our singleton with the arguments
    # we're not going to actually use singleArgParse
    singleBleMini = BleMini(args)

    # instead of using asyncio.run, just create a new BleakScanner 
    async with BleakScanner(singleBleMini.scanCallback):
        await singleBleMini.device_found_event.wait()

    # if we're here, the device has been found, scanning is stopped, now process the arguments
    await singleBleMini.connect()

if __name__ == "__main__":
    asyncio.run(main())

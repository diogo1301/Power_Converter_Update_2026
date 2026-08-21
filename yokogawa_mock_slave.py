import socket
import threading
import struct
import json
import os

HOST = "192.168.0.10"
PORT = 2000

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "registers.json")

DEVICE_ATTR = {
    'X': 0x0018, 'Y': 0x0019, 'I': 0x0009, 'E': 0x0005, 'M': 0x000D,
    'T': 0x0014, 'C': 0x0003, 'L': 0x000C, 'D': 0x0004, 'B': 0x0002,
    'F': 0x0006, 'R': 0x0012, 'V': 0x0016, 'Z': 0x001A, 'W': 0x0017,
}
ATTR_TO_TYPE = {v: k for k, v in DEVICE_ATTR.items()}

# Registres D 32 bits (mot bas -> mot bas+1 = mot haut), d'apres ND280PS_EPICS_record_r2.xlsx
LONG_REGISTERS_D = {87, 91, 94, 97, 105, 107, 109, 111, 113, 127, 131, 134, 140, 142}

lock = threading.Lock()
conn = None
last_command_log = "Aucune commande recue pour le moment."


def load_registers():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            print("Fichier de registres illisible, on repart de zero.")
    return {}


def save_registers():
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(registers, f)
    except OSError as e:
        print(f"Impossible de sauvegarder les registres : {e}")


registers = load_registers()  # cle: "D:87" -> valeur UInt16 (0-65535)


def reg_key(dtype, number):
    return f"{dtype}:{number}"


def get_word(dtype, number):
    return registers.get(reg_key(dtype, number), 0)


def set_word(dtype, number, value):
    registers[reg_key(dtype, number)] = value & 0xFFFF
    save_registers()


# ------------------------------------------------------------------
# Decodage / encodage des trames HLS binaires (meme logique que fam3_hls-e.py)
# ------------------------------------------------------------------

def unpack_device(raw):
    attr, number = struct.unpack(">HI", raw[:6])
    dtype = ATTR_TO_TYPE.get(attr, f"0x{attr:04X}")
    return dtype, number


def handle_command(raw):
    """Decode une commande WRD/WWR et construit la reponse binaire."""
    global last_command_log

    cmd_type, cpu_no, size = struct.unpack(">BBH", raw[0:4])
    params = raw[4:4 + size]

    if cmd_type == 0x11:  # WRD - Read words
        dtype, number = unpack_device(params[0:6])
        count = struct.unpack(">H", params[6:8])[0]
        values = [get_word(dtype, number + i) for i in range(count)]
        last_command_log = f"WRD lecture {dtype}{number:05d} x{count} -> {values}"
        resp_params = struct.pack(f">{count}H", *values)
        return struct.pack(">BBH", 0x91, 0x00, len(resp_params)) + resp_params

    elif cmd_type == 0x12:  # WWR - Write words
        dtype, number = unpack_device(params[0:6])
        count = struct.unpack(">H", params[6:8])[0]
        values = struct.unpack(f">{count}H", params[8:8 + count * 2])
        for i, v in enumerate(values):
            set_word(dtype, number + i, v)
        last_command_log = f"WWR ecriture {dtype}{number:05d} x{count} <- {list(values)}"
        return struct.pack(">BBH", 0x92, 0x00, 0)

    else:
        last_command_log = f"Commande non geree : 0x{cmd_type:02X}"
        return struct.pack(">BBH", cmd_type | 0x80, 0x02, 0)  # ER02: Command error


def receiver_thread(sock):
    buffer = b""
    while True:
        try:
            chunk = sock.recv(4096)
        except OSError:
            break
        if not chunk:
            print("\n[Connexion fermee par le PLC]")
            break
        buffer += chunk

        while len(buffer) >= 4:
            size = struct.unpack(">H", buffer[2:4])[0]
            total_len = 4 + size
            if len(buffer) < total_len:
                break  # trame incomplete, on attend la suite
            raw_cmd, buffer = buffer[:total_len], buffer[total_len:]

            with lock:
                response = handle_command(raw_cmd)
            if response:
                try:
                    sock.sendall(response)
                except OSError as e:
                    print(f"Erreur d'envoi de la reponse : {e}")

            print(f"\n[DEBUG] {last_command_log}")


def start_server():
    global conn
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((HOST, PORT))
    server.listen(1)
    print(f"En attente de connexion du PLC sur {HOST}:{PORT} ...")
    conn, addr = server.accept()
    print(f"PLC connecte depuis {addr}\n")
    threading.Thread(target=receiver_thread, args=(conn,), daemon=True).start()


# ------------------------------------------------------------------
# Menu interactif
# ------------------------------------------------------------------

def print_menu(title, options):
    print(f"\n--- {title} ---")
    for key in sorted(options):
        print(f"{key}. {options[key]}")
    return input("Choix : ").strip()


def view_register():
    dtype = input("Type de registre (ex: D, X, Y) : ").strip().upper()
    try:
        number = int(input("Numero de registre : ").strip())
    except ValueError:
        print("Numero invalide.")
        return

    with lock:
        val = get_word(dtype, number)

    if dtype == "D" and number in LONG_REGISTERS_D:
        with lock:
            high = get_word(dtype, number + 1)
        combined = (high << 16) | val
        print(f"{dtype}{number:05d} (mot bas)  = {val}")
        print(f"{dtype}{number + 1:05d} (mot haut) = {high}")
        print(f"Valeur 32 bits combinee     = {combined}")
    else:
        print(f"{dtype}{number:05d} = {val}")


def set_register():
    dtype = input("Type de registre (ex: D, X, Y) : ").strip().upper()
    try:
        number = int(input("Numero de registre : ").strip())
    except ValueError:
        print("Numero invalide.")
        return

    if dtype == "D" and number in LONG_REGISTERS_D:
        try:
            value32 = int(input(f"Valeur 32 bits a ecrire dans {dtype}{number:05d}/{number + 1:05d} : ").strip())
        except ValueError:
            print("Valeur invalide.")
            return
        low = value32 & 0xFFFF
        high = (value32 >> 16) & 0xFFFF
        with lock:
            set_word(dtype, number, low)
            set_word(dtype, number + 1, high)
        print(f"{dtype}{number:05d} = {low} (mot bas), {dtype}{number + 1:05d} = {high} (mot haut)")
    else:
        try:
            value = int(input(f"Valeur a ecrire dans {dtype}{number:05d} (0-65535) : ").strip())
            if not (0 <= value <= 65535):
                print("Valeur hors plage (0-65535).")
                return
        except ValueError:
            print("Valeur invalide.")
            return
        with lock:
            set_word(dtype, number, value)
        print(f"{dtype}{number:05d} = {value}")


def main_menu():
    while True:
        choice = print_menu("MENU PRINCIPAL", {
            "1": "Consulter un registre",
            "2": "Definir un registre",
            "3": "Voir la derniere commande recue",
            "0": "Quitter",
        })
        if choice == "1":
            view_register()
        elif choice == "2":
            set_register()
        elif choice == "3":
            print(f"\n{last_command_log}")
        elif choice == "0":
            print("Fermeture.")
            break
        else:
            print("Choix invalide.")


if __name__ == "__main__":
    start_server()
    main_menu()

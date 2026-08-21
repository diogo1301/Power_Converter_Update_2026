import socket
import threading
import struct
import time
import tkinter as tk

HOST = "172.16.0.10"
PORT = 2000

DEVICE_ATTR = {
    'X': 0x0018, 'Y': 0x0019, 'I': 0x0009, 'E': 0x0005, 'M': 0x000D,
    'T': 0x0014, 'C': 0x0003, 'L': 0x000C, 'D': 0x0004, 'B': 0x0002,
    'F': 0x0006, 'R': 0x0012, 'V': 0x0016, 'Z': 0x001A, 'W': 0x0017,
}
ATTR_TO_TYPE = {v: k for k, v in DEVICE_ATTR.items()}

lock = threading.Lock()
registers = {}          # cle "D:87" -> valeur brute UInt16
conn_holder = {"conn": None}

INTERLOCK_BIT_ORDER = [
    "over_current_1", "over_current_2", "over_current_3",
    "overvoltage_1", "overvoltage_2",
    "ground_fault", "outside_ground_fault",
    "fan1_stop", "fan2_stop", "fan3_stop", "fan_control_stop",
    "rectifier_u", "rectifier_v", "rectifier_w", "overheat",
]

INTERLOCK_LABELS = {
    "over_current_1": "Surintensite CC 1",
    "over_current_2": "Surintensite CC 2",
    "over_current_3": "Surintensite CC 3",
    "overvoltage_1": "Surtension CC 1",
    "overvoltage_2": "Surtension CC 2",
    "ground_fault": "Defaut de terre",
    "outside_ground_fault": "Defaut de terre externe",
    "fan1_stop": "Arret ventilateur plafond 1",
    "fan2_stop": "Arret ventilateur plafond 2",
    "fan3_stop": "Arret ventilateur plafond 3",
    "fan_control_stop": "Arret ventilateur circuit controle",
    "rectifier_u": "Erreur redresseur phase U",
    "rectifier_v": "Erreur redresseur phase V",
    "rectifier_w": "Erreur redresseur phase W",
    "overheat": "Surchauffe semi-conducteur",
}

# ------------------------------------------------------------------
# REGISTER_FIELDS : source unique de verite pour toutes les variables
# de statut AUTRES que le coeur physique (courant/tension/etat/Ilk2).
# Chaque entree decrit un registre D (ou une paire pour un dword).
#
# IMPORTANT : les couples (echelle, plage normale) marques
# confirmed=False sont des PLACEHOLDERS. Ils n'ont pas ete valides
# contre le manuel Yokogawa (IM34M06H24-01E / IM34M6P41-01E) et
# DOIVENT etre verifies avant toute utilisation pour la mise en
# service reelle. Seuls sont confirmes : l'echelle x10 du courant
# (D87/D94-95) et le ratio tension/courant 195V/3300A.
# ------------------------------------------------------------------

REGISTER_FIELDS = []


def _bit(id_, d, bit, category, subcat, label, default=False, confirmed=True, note=""):
    REGISTER_FIELDS.append({
        "id": id_, "kind": "bit", "d": d, "bit": bit,
        "category": category, "subcat": subcat, "label": label,
        "default": default, "confirmed": confirmed, "note": note,
    })


def _num(id_, d, words, category, subcat, label, unit, default, range_hint,
         confirmed=True, note=""):
    REGISTER_FIELDS.append({
        "id": id_, "kind": "num", "d": d, "words": words,
        "category": category, "subcat": subcat, "label": label,
        "unit": unit, "default": default, "range_hint": range_hint,
        "confirmed": confirmed, "note": note,
    })


def _bit_run(prefix, id_prefix, d_low, d_high, category, subcat, count, note=""):
    """Genere `count` bits consecutifs, 16 dans d_low puis le reste dans d_high."""
    for i in range(1, count + 1):
        bit = (i - 1) % 16
        d = d_low if i <= 16 else d_high
        _bit(f"{id_prefix}_{i:02d}", d, bit, category, subcat,
             f"{prefix} {i:02d}", confirmed=True, note=note)


# ---- Status general (D78, D83, D84, D93 bits hauts) ----
_bit("remote_mode", 78, 0, "Gen", "Status general",
     "Remote Mode (commande a distance active)")
_num("command_status", 83, 1, "Gen", "Status general", "Command Status (code retour)",
     "code", 0, "Format du code non documente", confirmed=False)
_bit("busy_flag", 84, 0, "Gen", "Status general", "Busy Flag")
_bit("output_polarity_positive", 93, 4, "Gen", "Status general",
     "Polarite de sortie positive (1=positive, 0=negative)", default=True)
_bit("sweep_stopped", 93, 5, "Gen", "Status general", "Sweep stoppe (Hold actif)")
_bit("set_value_limit_reached", 93, 6, "Gen", "Status general",
     "Limite de consigne atteinte")
_num("current_set_readback", 97, 2, "Gen", "Status general",
     "Current Set Readback (retour de consigne)", "A brut",
     0, "0-33000 brut = 0-3300A (echelle x10 supposee, comme D87)", confirmed=False)

# ---- Ilk1 : AC / Disjoncteurs (D148) ----
_ILK1 = [
    ("emergency_button", "Bouton d'arret d'urgence"),
    ("ac_over_under_voltage", "AC Surtension/Soustension"),
    ("acb_overcurrent_trip", "[ACB] Declenchement surintensite"),
    ("acb_overload_trip", "[ACB] Declenchement surcharge"),
    ("acb_short_circuit_trip", "[ACB] Declenchement court-circuit"),
    ("acb_leakage", "[ACB] Fuite"),
    ("acb_contacts_overheat", "[ACB] Surchauffe contacts circuit principal"),
    ("acb_relay_failure", "[ACB] Defaut relais declenchement surintensite"),
    ("ac400v_blackout", "[AC400V] Coupure alimentation principale"),
    ("elb2_leakage_trip", "[ELB2] Declenchement disjoncteur fuite alim. principale"),
    ("mcb2_fan_switch_trip", "[MCB2] Declenchement contacteur ventilateur plafond"),
    ("ac100v_blackout", "[AC100V] Coupure alimentation controle"),
    ("ac100v_leakage_trip", "[AC100V] Declenchement disjoncteur fuite alim. controle"),
    ("ups_abnormality", "[UPS] Alarme anomalie (batterie/surcharge/appareil)"),
]
for i, (key, label) in enumerate(_ILK1):
    _bit(key, 148, i, "Ilk1", "Alarmes AC / Disjoncteurs", label)

# ---- PowerMeter (D129 ADC failure, D130 = 16 bits) ----
_bit("pm_adc_failure", 129, 0, "PM", "Compteur de puissance", "Defaut ADC")
_PM = [
    "Erreur donnees systeme / calibration / parametres / backup",
    "Erreur EEPROM",
    "Depassement plage puissance instantanee",
    "Depassement plage puissance apparente instantanee",
    "Depassement plage puissance reactive instantanee",
    "Depassement plage courant instantane I1",
    "Depassement plage courant instantane I2",
    "Depassement plage courant instantane I3",
    "Depassement plage haute tension instantanee V1",
    "Depassement plage haute tension instantanee V2",
    "Depassement plage haute tension instantanee V3",
    "Depassement plage basse tension instantanee V1",
    "Depassement plage basse tension instantanee V2",
    "Depassement plage basse tension instantanee V3",
    "Depassement plage facteur de puissance",
    "Depassement plage frequence",
]
for i, label in enumerate(_PM):
    _bit(f"pm_err_{i:02d}", 130, i, "PM", "Compteur de puissance", label)

# ---- Ilk3-4 : IGBT Fault 1 (court-circuit), 22 bits, D150/D151 ----
_bit_run("IGBT_FAULT1", "igbt1", 150, 151, "Ilk3_4",
         "IGBT Fault 1 (detection court-circuit)", 22)

# ---- Ilk5-6 : IGBT Fault 2 (fusible), 22 bits, D152/D153 ----
_bit_run("IGBT_FAULT2", "igbt2", 152, 153, "Ilk5_6",
         "IGBT Fault 2 (detection fusible grille)", 22)

# ---- Ilk7-8 : Lf1 surchauffe (22 bits, D154/D155) + transfo/choke (D155 b6-b10) ----
_bit_run("Lf1", "lf1", 154, 155, "Ilk7_8",
         "Surchauffe bobine choke IGBT (Lf1)", 22)
_bit("t1_u_overheat", 155, 6, "Ilk7_8", "Transformateur / Choke", "T1-U surchauffe (bobine transfo)")
_bit("t1_v_overheat", 155, 7, "Ilk7_8", "Transformateur / Choke", "T1-V surchauffe (bobine transfo)")
_bit("t1_w_overheat", 155, 8, "Ilk7_8", "Transformateur / Choke", "T1-W surchauffe (bobine transfo)")
_bit("t1_core_overheat", 155, 9, "Ilk7_8", "Transformateur / Choke", "T1-CORE surchauffe (noyau transfo)")
_bit("l1_ohp", 155, 10, "Ilk7_8", "Transformateur / Choke", "L1-OHP (bobine choke)")

# ---- Ilk9 : cartes IGBT refroidies a eau (D156, 8 bits) ----
for i in range(1, 9):
    _bit(f"igbt_water_board_{i:02d}", 156, i - 1, "Ilk9",
         "Cartes IGBT refroidies a eau", f"Carte {i:02d} surchauffe")

# ---- Ilk10 : fuite/refroidissement (D157) ----
_bit("water_leak", 157, 0, "Ilk10_12", "Refroidissement (Ilk10)",
     "Fuite d'eau / capteur deconnecte")
_bit("coolant_temp_rise", 157, 1, "Ilk10_12", "Refroidissement (Ilk10)",
     "Elevation temperature liquide de refroidissement")
_bit("coolant_drop", 157, 2, "Ilk10_12", "Refroidissement (Ilk10)",
     "Baisse niveau liquide de refroidissement")
_bit("board_main_overheat", 157, 3, "Ilk10_12", "Refroidissement (Ilk10)",
     "Surchauffe carte (circuit principal)")
_bit("board_ctrl_overheat", 157, 4, "Ilk10_12", "Refroidissement (Ilk10)",
     "Surchauffe carte (circuit controle)")

# ---- Ilk11 : aimant (D158) ----
_bit("magnet_ready", 158, 0, "Ilk10_12", "Aimant (Ilk11)", "Magnet Ready", default=True)
_bit("magnet_cw_temp", 158, 1, "Ilk10_12", "Aimant (Ilk11)", "Magnet - temperature eau refroidissement")
_bit("magnet_cw_pressure", 158, 2, "Ilk10_12", "Aimant (Ilk11)", "Magnet - pression eau refroidissement")
_bit("ext_interlock", 158, 3, "Ilk10_12", "Aimant (Ilk11)", "Interlock externe")
_bit("ext_emergency", 158, 4, "Ilk10_12", "Aimant (Ilk11)", "Urgence externe")

# ---- Ilk12 : deviation / UPS (D159) ----
_bit("current_deviation", 159, 0, "Ilk10_12", "Deviation / UPS (Ilk12)", "Deviation de courant")
_bit("voltage_deviation", 159, 1, "Ilk10_12", "Deviation / UPS (Ilk12)", "Deviation de tension")
_bit("ups_battery_warning", 159, 2, "Ilk10_12", "Deviation / UPS (Ilk12)",
     "[UPS] Alerte decharge batterie")

# ---- Mesures analogiques : AC400V & Puissance ----
_num("ac400_r_current", 105, 2, "AC400", "Courants/tensions AC400V",
     "Courant AC400 phase R", "A brut", 0, "Echelle non documentee", confirmed=False)
_num("ac400_t_current", 107, 2, "AC400", "Courants/tensions AC400V",
     "Courant AC400 phase T", "A brut", 0, "Echelle non documentee", confirmed=False)
_num("ac400_r_voltage", 109, 2, "AC400", "Courants/tensions AC400V",
     "Tension AC400 phase R", "V brut", 0, "Echelle non documentee", confirmed=False)
_num("ac400_t_voltage", 111, 2, "AC400", "Courants/tensions AC400V",
     "Tension AC400 phase T", "V brut", 0,
     "Libelle document ambigu (R/R dans le PDF, corrige en T par analogie)",
     confirmed=False)
_num("active_power", 113, 2, "AC400", "Puissance", "Puissance active", "W brut",
     0, "Echelle non documentee", confirmed=False)
_num("apparent_power", 127, 2, "AC400", "Puissance", "Puissance apparente", "VA brut",
     0, "Echelle non documentee", confirmed=False)

# ---- Mesures analogiques : Refroidissement / Chassis ----
_num("cooling_water_temp_monitor", 99, 1, "Cool", "Eau de refroidissement",
     "Temperature eau (monitor)", "raw", 250,
     "Valeur d'exemple 'normale' - echelle/unite non confirmees", confirmed=False)
_num("cooling_water_flow_monitor", 100, 1, "Cool", "Eau de refroidissement",
     "Debit eau (monitor)", "raw", 100,
     "Valeur d'exemple 'normale' - echelle/unite non confirmees", confirmed=False)
_num("cooling_water_temp_threshold", 102, 1, "Cool", "Eau de refroidissement",
     "Seuil temperature eau", "raw", 350,
     "Devrait etre > au monitor en fonctionnement normal", confirmed=False)
_num("cooling_water_flow_threshold", 103, 1, "Cool", "Eau de refroidissement",
     "Seuil debit eau (minimum)", "raw", 50,
     "Devrait etre < au monitor en fonctionnement normal", confirmed=False)
_num("chassis_temp_main", 144, 1, "Cool", "Chassis", "Temperature chassis (circuit principal)",
     "raw", 250, "Valeur d'exemple - echelle non confirmee", confirmed=False)
_num("chassis_temp_main_threshold", 145, 1, "Cool", "Chassis",
     "Seuil temperature chassis (circuit principal)", "raw", 400,
     "Valeur d'exemple - echelle non confirmee", confirmed=False)
_num("chassis_temp_ctrl", 146, 1, "Cool", "Chassis", "Temperature chassis (circuit controle)",
     "raw", 250, "Valeur d'exemple - echelle non confirmee", confirmed=False)
_num("chassis_temp_ctrl_threshold", 147, 1, "Cool", "Chassis",
     "Seuil temperature chassis (circuit controle)", "raw", 400,
     "Valeur d'exemple - echelle non confirmee", confirmed=False)

# ---- Mesures analogiques : Deviation & Divers ----
_num("output_current_limit_readback", 131, 2, "Dev", "Divers",
     "Retour limite de courant de sortie", "A brut", 33000,
     "Devrait normalement refleter D91/D92 ecrit par le PLC", confirmed=True,
     note="Structure confirmee (miroir de D91/92), echelle supposee identique (x10)")
_num("sweep_speed_readback", 133, 1, "Dev", "Divers", "Retour vitesse de sweep",
     "raw", 0, "Echelle non documentee", confirmed=False)
_num("deviation_detect_start_current", 134, 2, "Dev", "Divers",
     "Courant de declenchement detection deviation", "A brut", 0,
     "Echelle non documentee", confirmed=False)
_num("deviation_current_set_value", 136, 1, "Dev", "Divers",
     "Consigne deviation de courant", "raw", 0, "Echelle non documentee", confirmed=False)
_num("deviation_voltage_set_value", 137, 1, "Dev", "Divers",
     "Consigne deviation de tension", "raw", 0, "Echelle non documentee", confirmed=False)
_num("deviation_judgement_start_time", 138, 1, "Dev", "Divers",
     "Temps de debut jugement deviation", "raw (s?)", 0,
     "Echelle/unite non documentees", confirmed=False)
_num("deviation_judgement_process_time", 139, 1, "Dev", "Divers",
     "Temps de traitement jugement deviation", "raw (s?)", 0,
     "Echelle/unite non documentees", confirmed=False)
_num("set_current_monitor_analog", 140, 2, "Dev", "Divers",
     "Monitor analogique consigne de courant", "A brut", 0,
     "Echelle non documentee", confirmed=False)
_num("current_feedback_dcct_monitor", 142, 2, "Dev", "Divers",
     "Monitor retour courant DCCT", "A brut", 0,
     "Echelle non documentee", confirmed=False)

INTERLOCK_CATEGORIES = [
    ("Status general", "Gen"),
    ("AC / Disjoncteurs (Ilk1)", "Ilk1"),
    ("Compteur de puissance (PowerMeter)", "PM"),
    ("IGBT Fault 1 (Ilk3-4)", "Ilk3_4"),
    ("IGBT Fault 2 (Ilk5-6)", "Ilk5_6"),
    ("Thermique bobines/transfo (Ilk7-8)", "Ilk7_8"),
    ("Cartes IGBT eau (Ilk9)", "Ilk9"),
    ("Refroid./Aimant/Deviation (Ilk10-12)", "Ilk10_12"),
]

ANALOG_CATEGORIES = [
    ("General", "Gen"),
    ("AC400V & Puissance", "AC400"),
    ("Refroidissement / Chassis", "Cool"),
    ("Deviation & Divers", "Dev"),
]

# D93 est partage entre la physique (bits 0,1,2) et des bits manuels
# (bits 4,5,6) - on l'exclut de l'ecriture generique et on le fusionne
# explicitement dans simulation_tick().
MANUAL_BIT_REGS_HANDLED_ELSEWHERE = frozenset({93})


# ------------------------------------------------------------------
# Etat de simulation partage (proteger par 'lock' pour tout acces)
# ------------------------------------------------------------------

sim = {
    "state": "STOP",           # STOP, RAMP_UP, RUN, RAMP_DOWN, FAULT
    "actual_current": 0.0,     # A - valeur physique simulee
    "target_current": 0.0,     # A - consigne appliquee (depuis D00087/88)
    "voltage": 0.0,            # V - calculee a partir du courant
    "output_on": False,
    "holding": False,
    "ramp_rate": 50.0,         # A/s - vitesse de montee/descente, ajustable via l'IHM
    "scale": 10,               # facteur d'echelle registre (confirme pour D87/D94-95)
    # Ratio tension/courant confirme via la doc Yokogawa : 195V / 3300A
    "load_ohm": 195.0 / 3300.0,
    "interlocks": {name: False for name in INTERLOCK_BIT_ORDER},
    "manual_bits": {f["id"]: f["default"] for f in REGISTER_FIELDS if f["kind"] == "bit"},
    "manual_num": {f["id"]: f["default"] for f in REGISTER_FIELDS if f["kind"] == "num"},
}


# ------------------------------------------------------------------
# Acces bas niveau aux registres simules
# ------------------------------------------------------------------

def reg_key(dtype, number):
    return f"{dtype}:{number}"


def get_word(dtype, number):
    return registers.get(reg_key(dtype, number), 0)


def set_word(dtype, number, value):
    registers[reg_key(dtype, number)] = value & 0xFFFF


def get_dword(number):
    return (get_word("D", number + 1) << 16) | get_word("D", number)


def set_dword(number, value):
    value &= 0xFFFFFFFF
    set_word("D", number, value & 0xFFFF)
    set_word("D", number + 1, (value >> 16) & 0xFFFF)


def write_manual_registers():
    """Ecrit dans les registres D toutes les valeurs manuelles definies
    dans REGISTER_FIELDS. Doit etre appelee sous 'lock'. D93 est exclu
    (fusionne a part car partage avec la physique)."""
    bit_words = {}
    all_bit_regs = set()
    for f in REGISTER_FIELDS:
        if f["kind"] != "bit":
            continue
        all_bit_regs.add(f["d"])
        if f["d"] in MANUAL_BIT_REGS_HANDLED_ELSEWHERE:
            continue
        if sim["manual_bits"].get(f["id"], f["default"]):
            bit_words[f["d"]] = bit_words.get(f["d"], 0) | (1 << f["bit"])
    for d in all_bit_regs:
        if d in MANUAL_BIT_REGS_HANDLED_ELSEWHERE:
            continue
        set_word("D", d, bit_words.get(d, 0))

    for f in REGISTER_FIELDS:
        if f["kind"] != "num":
            continue
        raw = sim["manual_num"].get(f["id"], f["default"])
        try:
            raw = int(raw)
        except (TypeError, ValueError):
            raw = f["default"]
        if f["words"] == 2:
            set_dword(f["d"], raw & 0xFFFFFFFF)
        else:
            set_word("D", f["d"], raw & 0xFFFF)


# ------------------------------------------------------------------
# Simulation physique (thread dedie, tick periodique)
# ------------------------------------------------------------------

def simulation_tick(dt):
    with lock:
        # Handshake de commande D00082 -> traite D00081 puis s'auto-efface (0)
        if get_word("D", 82) == 1:
            cmd = get_word("D", 81)
            if cmd & 0x08:      # bit3 : Output ON
                sim["output_on"] = True
            if cmd & 0x02:      # bit1 : Output OFF
                sim["output_on"] = False
            if cmd & 0x10:      # bit4 : reset defaut externe
                if not any(sim["interlocks"].values()):
                    sim["state"] = "STOP"
            set_word("D", 82, 0)

        # Application de la consigne D00089 -> lit D00087/88 puis s'auto-efface
        # Plage officielle documentee pour ce modele (3300A) : 0-33000 brut = 0.0-3300.0A
        if get_word("D", 89) == 1:
            raw_val = max(0, min(get_dword(87), 33000))
            sim["target_current"] = raw_val / sim["scale"]
            set_word("D", 89, 0)

        # D00091/92 (limite de courant) : documente comme "non utilise" sur ce modele.
        # Reste normalement lisible/inscriptible (comme n'importe quel registre),
        # mais aucune logique de simulation ne s'appuie dessus - sans effet volontaire.

        # Hold (gel de la rampe) D00090 - declencheur ponctuel, comme SET (D00089) :
        # chaque transition vers 1 bascule l'etat de gel, puis le registre s'auto-efface
        if get_word("D", 90) == 1:
            sim["holding"] = not sim["holding"]
            set_word("D", 90, 0)

        active_fault = any(sim["interlocks"].values())
        if active_fault:
            sim["state"] = "FAULT"
            sim["output_on"] = False

        if sim["state"] != "FAULT":
            desired = sim["target_current"] if sim["output_on"] else 0.0
            if not sim["holding"]:
                step = sim["ramp_rate"] * dt
                if sim["actual_current"] < desired:
                    sim["actual_current"] = min(sim["actual_current"] + step, desired)
                elif sim["actual_current"] > desired:
                    sim["actual_current"] = max(sim["actual_current"] - step, desired)

            if sim["output_on"]:
                sim["state"] = "RUN" if abs(sim["actual_current"] - sim["target_current"]) < 0.05 else "RAMP_UP"
            else:
                sim["state"] = "STOP" if sim["actual_current"] <= 0.05 else "RAMP_DOWN"

        # V = I x R, ratio confirme 195V/3300A. Ecrit avec la meme echelle x10
        # que le courant (HYPOTHESE non confirmee dans le manuel pour ce registre).
        sim["voltage"] = sim["actual_current"] * sim["load_ohm"]

        # --- Registres de statut renvoyes au PLC ---
        status = 0
        if sim["state"] == "STOP":
            status |= (1 << 1)
        if sim["state"] == "RUN":
            status |= (1 << 3)
        if sim["state"] == "FAULT":
            status |= (1 << 4)
        set_word("D", 79, status)

        status2 = (1 << 1)
        if sim["output_on"]:
            status2 |= (1 << 0) | (1 << 2)
        # Fusion des bits manuels de D93 (polarite / sweep / limite atteinte)
        manual_d93 = 0
        for f in REGISTER_FIELDS:
            if f["kind"] == "bit" and f["d"] == 93:
                if sim["manual_bits"].get(f["id"], f["default"]):
                    manual_d93 |= (1 << f["bit"])
        set_word("D", 93, status2 | manual_d93)

        set_dword(94, int(round(sim["actual_current"] * sim["scale"])))
        raw_voltage = int(round(sim["voltage"] * sim["scale"]))
        set_word("D", 96, max(0, min(raw_voltage, 65535)))

        ilk_word = 0
        for i, name in enumerate(INTERLOCK_BIT_ORDER):
            if sim["interlocks"][name]:
                ilk_word |= (1 << i)
        set_word("D", 149, ilk_word)

        # Toutes les autres variables (interlocks Ilk1/3-12/PowerMeter,
        # mesures analogiques etc.) - valeurs manuelles definies via l'IHM
        write_manual_registers()


def physics_thread():
    last = time.time()
    while True:
        time.sleep(0.1)
        now = time.time()
        dt = now - last
        last = now
        simulation_tick(dt)


# ------------------------------------------------------------------
# Protocole HLS binaire (WRD/WWR) - identique a fam3_hls-e.py
# ------------------------------------------------------------------

def unpack_device(raw):
    attr, number = struct.unpack(">HI", raw[:6])
    dtype = ATTR_TO_TYPE.get(attr, f"0x{attr:04X}")
    return dtype, number


def handle_command(raw):
    cmd_type, cpu_no, size = struct.unpack(">BBH", raw[0:4])
    params = raw[4:4 + size]

    if cmd_type == 0x11:  # WRD
        dtype, number = unpack_device(params[0:6])
        count = struct.unpack(">H", params[6:8])[0]
        values = [get_word(dtype, number + i) for i in range(count)]
        resp_params = struct.pack(f">{count}H", *values)
        return struct.pack(">BBH", 0x91, 0x00, len(resp_params)) + resp_params

    elif cmd_type == 0x12:  # WWR
        dtype, number = unpack_device(params[0:6])
        count = struct.unpack(">H", params[6:8])[0]
        values = struct.unpack(f">{count}H", params[8:8 + count * 2])
        for i, v in enumerate(values):
            set_word(dtype, number + i, v)
        return struct.pack(">BBH", 0x92, 0x00, 0)

    else:
        return struct.pack(">BBH", cmd_type | 0x80, 0x02, 0)


def handle_connection(sock):
    buffer = b""
    while True:
        try:
            chunk = sock.recv(4096)
        except OSError:
            break
        if not chunk:
            print("[Connexion fermee par le PLC]")
            break
        buffer += chunk
        while len(buffer) >= 4:
            size = struct.unpack(">H", buffer[2:4])[0]
            total_len = 4 + size
            if len(buffer) < total_len:
                break
            raw_cmd, buffer = buffer[:total_len], buffer[total_len:]
            with lock:
                response = handle_command(raw_cmd)
            if response:
                try:
                    sock.sendall(response)
                except OSError as e:
                    print(f"Erreur d'envoi de la reponse : {e}")


def start_server():
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((HOST, PORT))
    server.listen(1)
    print(f"En attente de connexion du PLC sur {HOST}:{PORT} ...")

    def accept_loop():
        while True:
            conn, addr = server.accept()
            conn_holder["conn"] = conn
            print(f"PLC connecte depuis {addr}")
            handle_connection(conn)
            conn_holder["conn"] = None

    threading.Thread(target=accept_loop, daemon=True).start()


# ------------------------------------------------------------------
# Interface graphique (Tkinter)
# ------------------------------------------------------------------

BG = "#1a1a1a"
FG_AMBER = "#ffb300"
FG_GREEN = "#4caf50"
FG_GRAY = "#9e9e9e"
FG_RED = "#e53935"
FG_DIM = "#666666"
FONT_BIG = ("Consolas", 26, "bold")
FONT_MED = ("Consolas", 13)
FONT_SMALL = ("Consolas", 10)

STATE_COLORS = {"STOP": FG_GRAY, "RUN": FG_GREEN, "RAMP_UP": FG_AMBER,
                 "RAMP_DOWN": FG_AMBER, "FAULT": FG_RED}
STATE_LABELS = {"STOP": "ARRETE", "RUN": "EN MARCHE", "RAMP_UP": "MONTEE EN COURANT",
                 "RAMP_DOWN": "DESCENTE EN COURANT", "FAULT": "DEFAUT"}


class ScrollFrame(tk.Frame):
    """Zone defilante (molette active seulement quand la souris est dessus)."""

    def __init__(self, parent, **kw):
        super().__init__(parent, bg=BG, **kw)
        self.canvas = tk.Canvas(self, bg=BG, highlightthickness=0)
        scrollbar = tk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.inner = tk.Frame(self.canvas, bg=BG)
        self.inner.bind("<Configure>",
                         lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.canvas.configure(yscrollcommand=scrollbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        def _wheel(event):
            self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

        self.canvas.bind("<Enter>", lambda e: self.canvas.bind_all("<MouseWheel>", _wheel))
        self.canvas.bind("<Leave>", lambda e: self.canvas.unbind_all("<MouseWheel>"))


class TabbedPanel(tk.Frame):
    """Barre d'onglets simple (style coherent avec le theme sombre de l'appli),
    tabs: liste de (label, build_fn) ou build_fn(container) -> Frame."""

    def __init__(self, parent, tabs):
        super().__init__(parent, bg=BG)
        btn_row = tk.Frame(self, bg=BG)
        btn_row.pack(fill="x", pady=(0, 8))
        self.container = tk.Frame(self, bg=BG)
        self.container.pack(fill="both", expand=True)
        self.frames = {}
        self.buttons = {}
        for label, build_fn in tabs:
            f = build_fn(self.container)
            f.place(in_=self.container, x=0, y=0, relwidth=1, relheight=1)
            self.frames[label] = f
            b = tk.Button(btn_row, text=label, font=("Consolas", 8), wraplength=140,
                          command=lambda l=label: self.show(l))
            b.pack(side="left", padx=2, pady=2)
            self.buttons[label] = b
        self.show(tabs[0][0])

    def show(self, label):
        self.frames[label].tkraise()
        for l, b in self.buttons.items():
            if l == label:
                b.config(relief="sunken", bg="#3a3a3a", fg=FG_AMBER)
            else:
                b.config(relief="raised", bg="#2a2a2a", fg="white")


def build_bitgroup_frame(parent, fields):
    sf = ScrollFrame(parent)
    vars_ = {}

    def make_toggle(fid, var):
        def _cb():
            with lock:
                sim["manual_bits"][fid] = var.get()
        return _cb

    def reset_all():
        with lock:
            for f in fields:
                sim["manual_bits"][f["id"]] = False
        for fid, v in vars_.items():
            v.set(False)

    top = tk.Frame(sf.inner, bg=BG)
    top.grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 10))
    tk.Button(top, text="Tout remettre a 0 (normal)", font=FONT_SMALL,
              command=reset_all).pack(side="left")

    row = 1
    last_subcat = None
    for f in fields:
        if f["subcat"] != last_subcat:
            last_subcat = f["subcat"]
            tk.Label(sf.inner, text=last_subcat, bg=BG, fg=FG_AMBER,
                     font=("Consolas", 11, "bold")).grid(
                row=row, column=0, columnspan=2, sticky="w", pady=(10, 2))
            row += 1
        var = tk.BooleanVar(value=sim["manual_bits"].get(f["id"], f["default"]))
        vars_[f["id"]] = var
        cb = tk.Checkbutton(sf.inner, text=f["label"], variable=var, bg=BG, fg="white",
                             selectcolor="#333333", activebackground=BG, font=FONT_SMALL,
                             anchor="w", command=make_toggle(f["id"], var))
        cb.grid(row=row, column=0, sticky="w", padx=20)
        if not f.get("confirmed", True):
            tk.Label(sf.inner, text="[non confirme]", bg=BG, fg=FG_DIM,
                     font=("Consolas", 8, "italic")).grid(row=row, column=1, sticky="w")
        row += 1
    return sf


def build_numgroup_frame(parent, fields):
    sf = ScrollFrame(parent)
    entries = {}

    def commit(fid, entry, default):
        def _c(event=None):
            txt = entry.get().strip()
            try:
                val = int(txt)
            except ValueError:
                val = default
            entry.delete(0, tk.END)
            entry.insert(0, str(val))
            with lock:
                sim["manual_num"][fid] = val
        return _c

    def reset_all():
        with lock:
            for f in fields:
                sim["manual_num"][f["id"]] = f["default"]
        for f in fields:
            e = entries[f["id"]]
            e.delete(0, tk.END)
            e.insert(0, str(f["default"]))

    top = tk.Frame(sf.inner, bg=BG)
    top.grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 10))
    tk.Button(top, text="Reinitialiser cet onglet", font=FONT_SMALL,
              command=reset_all).pack(side="left")

    row = 1
    last_subcat = None
    for f in fields:
        if f["subcat"] != last_subcat:
            last_subcat = f["subcat"]
            tk.Label(sf.inner, text=last_subcat, bg=BG, fg=FG_AMBER,
                     font=("Consolas", 11, "bold")).grid(
                row=row, column=0, columnspan=4, sticky="w", pady=(10, 2))
            row += 1
        tk.Label(sf.inner, text=f["label"], bg=BG, fg=FG_GRAY, font=FONT_SMALL,
                 anchor="w", width=38).grid(row=row, column=0, sticky="w", padx=(20, 5))
        e = tk.Entry(sf.inner, width=10, font=FONT_SMALL, bg="#222222", fg=FG_AMBER,
                     insertbackground="white")
        e.insert(0, str(sim["manual_num"].get(f["id"], f["default"])))
        e.grid(row=row, column=1, sticky="w")
        e.bind("<Return>", commit(f["id"], e, f["default"]))
        e.bind("<FocusOut>", commit(f["id"], e, f["default"]))
        entries[f["id"]] = e
        tk.Label(sf.inner, text=f.get("unit", ""), bg=BG, fg=FG_GRAY,
                 font=FONT_SMALL).grid(row=row, column=2, sticky="w", padx=(5, 10))
        hint = f.get("range_hint", "")
        if not f.get("confirmed", True):
            hint = (hint + "  " if hint else "") + "[non confirme]"
        tk.Label(sf.inner, text=hint, bg=BG, fg=FG_DIM, font=("Consolas", 8, "italic"),
                 wraplength=260, justify="left").grid(row=row, column=3, sticky="w")
        row += 1
    return sf


def build_ilk2_tab(parent):
    """Reproduit a l'identique l'ancien ecran d'interlocks DC + reglage de rampe."""
    frame = tk.Frame(parent, bg=BG)
    tk.Label(frame, text="Interlocks DC (Ilk2 - D00149) et reglages", bg=BG, fg=FG_AMBER,
              font=("Consolas", 13, "bold")).pack(pady=(10, 10))

    grid = tk.Frame(frame, bg=BG)
    grid.pack(pady=5)
    ilk2_vars = {}
    for i, name in enumerate(INTERLOCK_BIT_ORDER):
        var = tk.BooleanVar(value=sim["interlocks"][name])
        ilk2_vars[name] = var
        cb = tk.Checkbutton(grid, text=INTERLOCK_LABELS[name], variable=var, bg=BG, fg="white",
                             selectcolor="#333333", activebackground=BG, font=FONT_SMALL,
                             command=lambda n=name, v=var: _toggle_ilk2(n, v))
        cb.grid(row=i % 8, column=i // 8, sticky="w", padx=10, pady=2)

    ramp_frame = tk.Frame(frame, bg=BG)
    ramp_frame.pack(pady=15)
    tk.Label(ramp_frame, text="Vitesse de rampe (A/s) :", bg=BG, fg=FG_GRAY,
              font=FONT_SMALL).grid(row=0, column=0, padx=5)
    ramp_entry = tk.Entry(ramp_frame, width=8, font=FONT_SMALL)
    ramp_entry.insert(0, str(sim["ramp_rate"]))
    ramp_entry.grid(row=0, column=1, padx=5)

    def apply_ramp():
        try:
            value = float(ramp_entry.get())
            if value > 0:
                with lock:
                    sim["ramp_rate"] = value
        except ValueError:
            pass

    tk.Button(ramp_frame, text="Appliquer", font=FONT_SMALL,
              command=apply_ramp).grid(row=0, column=2, padx=5)
    return frame


def _toggle_ilk2(name, var):
    with lock:
        sim["interlocks"][name] = var.get()


class HomeFrame(tk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent, bg=BG)
        tk.Label(self, text="ND280 POWER CONVERTER", bg=BG, fg=FG_AMBER,
                 font=("Consolas", 16, "bold")).pack(pady=(20, 5))

        self.conn_lbl = tk.Label(self, text="", bg=BG, font=FONT_SMALL)
        self.conn_lbl.pack()

        self.state_lbl = tk.Label(self, text="", bg=BG, font=("Consolas", 17, "bold"))
        self.state_lbl.pack(pady=10)

        grid = tk.Frame(self, bg=BG)
        grid.pack(pady=10)
        self.voltage_lbl = self._readout(grid, "TENSION", 0)
        self.current_lbl = self._readout(grid, "COURANT", 1)
        self.target_lbl = self._readout(grid, "CONSIGNE (PLC)", 2)

        btn_frame = tk.Frame(self, bg=BG)
        btn_frame.pack(pady=25)
        tk.Button(btn_frame, text="Valeurs recues du PLC >", font=FONT_MED,
                  command=lambda: app.show_frame(PLCWritesFrame)).grid(
            row=0, column=0, padx=8, pady=5, sticky="ew")
        tk.Button(btn_frame, text="Interlocks & Alarmes >", font=FONT_MED,
                  command=lambda: app.show_frame(InterlocksFrame)).grid(
            row=0, column=1, padx=8, pady=5, sticky="ew")
        tk.Button(btn_frame, text="Mesures analogiques avancees >", font=FONT_MED,
                  command=lambda: app.show_frame(AnalogFrame)).grid(
            row=1, column=0, columnspan=2, padx=8, pady=5, sticky="ew")

    def _readout(self, parent, label, row):
        tk.Label(parent, text=label, bg=BG, fg=FG_GRAY, font=FONT_SMALL).grid(
            row=row, column=0, sticky="e", padx=10, pady=6)
        val = tk.Label(parent, text="---", bg=BG, fg=FG_AMBER, font=FONT_BIG)
        val.grid(row=row, column=1, sticky="w", padx=10)
        return val

    def refresh(self):
        with lock:
            state = sim["state"]
            v = sim["voltage"]
            i = sim["actual_current"]
            t = sim["target_current"]
        connected = conn_holder["conn"] is not None
        self.conn_lbl.config(text="PLC connecte" if connected else "En attente de connexion...",
                              fg=FG_GREEN if connected else FG_GRAY)
        self.state_lbl.config(text=STATE_LABELS[state], fg=STATE_COLORS[state])
        self.voltage_lbl.config(text=f"{v:7.1f} V")
        self.current_lbl.config(text=f"{i:7.1f} A")
        self.target_lbl.config(text=f"{t:7.1f} A")


class PLCWritesFrame(tk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent, bg=BG)
        tk.Label(self, text="Valeurs ecrites par le PLC Siemens", bg=BG, fg=FG_AMBER,
                 font=("Consolas", 14, "bold")).pack(pady=(20, 10))

        fields = [
            ("D00033 (INIT_Flag)", "init"),
            ("D00081 (CMD)", "cmd"),
            ("D00082 (cmd_hs)", "hs"),
            ("D00087/88 (consigne courant)", "setval"),
            ("D00089 (SET)", "set89"),
            ("D00090 (HOLD)", "hold"),
            ("D00091/92 (limite courant)", "limit"),
        ]
        self.rows = {}
        grid = tk.Frame(self, bg=BG)
        grid.pack(pady=10)
        for row, (label, key) in enumerate(fields):
            tk.Label(grid, text=label, bg=BG, fg=FG_GRAY, font=FONT_SMALL, anchor="w", width=30
                     ).grid(row=row, column=0, sticky="w", pady=4)
            val = tk.Label(grid, text="---", bg=BG, fg=FG_AMBER, font=("Consolas", 12, "bold"), width=12)
            val.grid(row=row, column=1, sticky="w")
            self.rows[key] = val

        tk.Button(self, text="< Retour", font=FONT_MED,
                  command=lambda: app.show_frame(HomeFrame)).pack(pady=20)

    def refresh(self):
        with lock:
            init_flag = get_word("D", 33)
            cmd = get_word("D", 81)
            hs = get_word("D", 82)
            setval = get_dword(87) / sim["scale"]
            set89 = get_word("D", 89)
            hold = get_word("D", 90)
            limit = get_dword(91) / sim["scale"]
        self.rows["init"].config(text=f"0x{init_flag:04X}")
        self.rows["cmd"].config(text=f"0x{cmd:04X}")
        self.rows["hs"].config(text=str(hs))
        self.rows["setval"].config(text=f"{setval:.1f} A")
        self.rows["set89"].config(text=str(set89))
        self.rows["hold"].config(text=str(hold))
        self.rows["limit"].config(text=f"{limit:.1f} A")


class InterlocksFrame(tk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent, bg=BG)
        tk.Label(self, text="Interlocks & Alarmes (avance)", bg=BG, fg=FG_AMBER,
                 font=("Consolas", 15, "bold")).pack(pady=(12, 2))
        tk.Label(self,
                 text="Bits marques [non confirme] : plage/echelle non validee contre le manuel.",
                 bg=BG, fg="#998800", font=("Consolas", 9, "italic")).pack(pady=(0, 8))

        tabs = [("DC (Ilk2)", build_ilk2_tab)]
        for label, code in INTERLOCK_CATEGORIES:
            fields = [f for f in REGISTER_FIELDS if f["kind"] == "bit" and f["category"] == code]
            tabs.append((label, lambda p, flds=fields: build_bitgroup_frame(p, flds)))

        self.panel = TabbedPanel(self, tabs)
        self.panel.pack(fill="both", expand=True, padx=10, pady=5)

        tk.Button(self, text="< Retour", font=FONT_MED,
                  command=lambda: app.show_frame(HomeFrame)).pack(pady=10)

    def refresh(self):
        pass


class AnalogFrame(tk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent, bg=BG)
        tk.Label(self, text="Mesures analogiques avancees", bg=BG, fg=FG_AMBER,
                 font=("Consolas", 15, "bold")).pack(pady=(12, 2))
        tk.Label(self,
                 text="Valeurs brutes de registre. [non confirme] = echelle/plage non validee.",
                 bg=BG, fg="#998800", font=("Consolas", 9, "italic")).pack(pady=(0, 8))

        tabs = []
        for label, code in ANALOG_CATEGORIES:
            fields = [f for f in REGISTER_FIELDS if f["kind"] == "num" and f["category"] == code]
            tabs.append((label, lambda p, flds=fields: build_numgroup_frame(p, flds)))

        self.panel = TabbedPanel(self, tabs)
        self.panel.pack(fill="both", expand=True, padx=10, pady=5)

        tk.Button(self, text="< Retour", font=FONT_MED,
                  command=lambda: app.show_frame(HomeFrame)).pack(pady=10)

    def refresh(self):
        pass


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("ND280PS - Simulateur")
        self.geometry("640x560")
        self.configure(bg=BG)

        container = tk.Frame(self, bg=BG)
        container.pack(fill="both", expand=True)
        container.grid_rowconfigure(0, weight=1)
        container.grid_columnconfigure(0, weight=1)

        self.frames = {}
        for F in (HomeFrame, PLCWritesFrame, InterlocksFrame, AnalogFrame):
            frame = F(container, self)
            self.frames[F] = frame
            frame.grid(row=0, column=0, sticky="nsew")

        self.show_frame(HomeFrame)
        self.after(300, self.refresh_loop)

    def show_frame(self, cls):
        self.frames[cls].tkraise()

    def refresh_loop(self):
        for f in self.frames.values():
            if hasattr(f, "refresh"):
                f.refresh()
        self.after(300, self.refresh_loop)


if __name__ == "__main__":
    threading.Thread(target=physics_thread, daemon=True).start()
    start_server()
    app = App()
    app.mainloop()

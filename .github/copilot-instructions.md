# Copilot Instructions for dbus-serialbattery (Seplos v3 Fork)

## ⚠️ Repository-Fokus

**Dieses Repository dient ausschließlich der Anpassung von `bms/seplosv3.py`.**  
Alle anderen Dateien sollen unverändert vom [Original-Repository](https://github.com/mr-manuel/venus-os_dbus-serialbattery) übernommen werden.

## Test-Umgebung

⚠️ **ACHTUNG: Test = Produktion!** Der Cerbo steuert ein Live-System. Fehlerhafte Änderungen können die Batterie-Steuerung beeinträchtigen.

- **Cerbo GX**: `ssh cerbo` (konfiguriert in `~/.ssh/config` mit Keep-Alive & Timeout)
- Driver-Pfad auf Cerbo: `/data/apps/dbus-serialbattery/`
- Logs anzeigen: `ssh cerbo "tail -80 /data/log/dbus-serialbattery.*/current | tai64nlocal"`
- Service neustarten: `ssh cerbo "/data/apps/dbus-serialbattery/restart.sh"`

### Vor dem Deployment

1. Code lokal mit `black` und `flake8` prüfen
2. Syntax-Check: `python -m py_compile dbus-serialbattery/bms/seplosv3.py`
3. Bei größeren Änderungen: Backup der aktuellen Version auf Cerbo erstellen

## Seplosv3-Spezifika

Die `seplosv3.py` nutzt **Modbus RTU** via `minimalmodbus`:

- Baudrate: 19200, Parity: None, Stopbits: 1
- Slave-Adressen: 0-15 (Auto-Detection wenn nicht konfiguriert)
- Identifikation via Register `0x1700` (Factory: "XZH-ElecTech Co.,Ltd")

### Wichtige Register-Bereiche

| Bereich | Register      | Inhalt                                                  |
| ------- | ------------- | ------------------------------------------------------- |
| SPA     | 0x1300-0x136A | System-Parameter (cell_count, capacity, voltage limits) |
| PIA     | 0x1000-0x1012 | Pack-Info (voltage, current, soc, cycles)               |
| PIB     | 0x1100-0x111A | Zellspannungen + Temperaturen                           |
| SFA     | 0x1400 (Bits) | Alarm-Flags                                             |
| PIC     | 0x1200 (Bits) | Control-Status (FETs, Balancing)                        |

## Projekt-Architektur (Überblick)

- **`battery.py`**: Abstrakte Basisklasse - `Seplosv3` erbt davon
- **`dbus-serialbattery.py`**: Entry point, ruft `test_connection()` → `refresh_data()` auf
- **`dbushelper.py`**: Publiziert Daten zum Venus OS dbus

### BMS Driver Pattern

Each BMS driver in `bms/` extends `Battery` and must implement:

```python
class YourBMS(Battery):
    def __init__(self, port, baud, address):
        super().__init__(port, baud, address)
        self.type = "YourBMSName"  # Shown in GUI

    def test_connection(self) -> bool:
        """Return True if BMS responds correctly. Called during auto-detection."""
        result = self.get_settings()
        return result and self.refresh_data()

    def get_settings(self) -> bool:
        """Initialize cell_count, capacity, cells array. Called once after connection."""
        self.cell_count = VALUE_FROM_BMS
        self.capacity = VALUE_FROM_BMS
        for _ in range(self.cell_count):
            self.cells.append(Cell(False))
        return True

    def refresh_data(self) -> bool:
        """Update voltage, current, soc, temperatures, cell voltages. Called every poll_interval."""
        # Set mandatory: self.voltage, self.current, self.soc, self.charge_fet, self.discharge_fet
        # Set temperatures via self.to_temperature(sensor_number, value)
        return True
```

See [bms/battery_template.py](dbus-serialbattery/bms/battery_template.py) for a complete template with all optional fields.

### Data Flow

1. BMS driver reads serial/BLE/CAN data → populates `Battery` fields
2. `dbushelper.publish_battery()` calls `refresh_data()`
3. `dbushelper.publish_dbus()` pushes values to Venus OS dbus
4. Victron system uses `/Info/MaxChargeCurrent`, `/Info/MaxDischargeCurrent`, `/Info/MaxChargeVoltage`

## Entwicklungs-Workflow

### ⚠️ SSH-Hinweis

Der Cerbo GX verträgt **nur eine SSH-Verbindung gleichzeitig**. Mehrere parallele Sessions oder hängende Verbindungen führen dazu, dass der Cerbo für SSH komplett blockiert wird und nur durch einen Neustart wieder erreichbar ist.

**Regeln für SSH-Zugriffe:**

- Immer `ssh cerbo` verwenden (nicht `ssh root@192.168.105.57`) – die SSH-Config enthält Keep-Alive und Timeout
- **Niemals** mehrere `ssh`/`scp`-Befehle parallel starten
- Immer auf Abschluss eines SSH-Befehls warten, bevor der nächste gestartet wird
- **Niemals `tail -f` oder andere dauerhaft laufende Befehle verwenden!**
  Wenn die Verbindung abbricht, bleibt der Prozess auf dem Cerbo als Zombie hängen und blockiert neue SSH-Verbindungen.
- Stattdessen immer **`tail -N`** (z.B. `tail -80`) verwenden – Befehle müssen sich selbst beenden!
- Mehrere Befehle in **einem einzigen SSH-Aufruf** zusammenfassen:
  ```bash
  ssh root@192.168.105.57 "BEFEHL1 && BEFEHL2 && BEFEHL3"
  ```

### Standard-Befehle

```bash
# Datei auf Cerbo kopieren
scp dbus-serialbattery/bms/seplosv3.py cerbo:/data/apps/dbus-serialbattery/bms/

# Service neustarten
ssh cerbo "/data/apps/dbus-serialbattery/restart.sh"

# Logs lesen (KEIN tail -f! Immer eine feste Anzahl Zeilen)
ssh cerbo "tail -80 /data/log/dbus-serialbattery.*/current | tai64nlocal"

# Deploy + Restart + Logs in einem Aufruf
scp dbus-serialbattery/bms/seplosv3.py cerbo:/data/apps/dbus-serialbattery/bms/
# Warten bis scp fertig, dann:
ssh cerbo "/data/apps/dbus-serialbattery/restart.sh && sleep 15 && tail -80 /data/log/dbus-serialbattery.*/current | tai64nlocal"
```

## Code Style

- **Line length**: 160 Zeichen (`pyproject.toml`)
- Formatter: Black, Linter: Flake8
- `ext/`-Ordner von Linting ausschließen

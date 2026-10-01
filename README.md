# 2N EasyGate (GSM brána) → Home Assistant

> **Stav:** ve vývoji. Ověřeno na jedné bráně 2N EasyGate (Siemens MC55i, firmware 01.003) s Home Assistant 2026.9.
> **Status:** work in progress, tested on a single 2N EasyGate unit. Documentation is in Czech for now.

**Instalace přes HACS:** HACS → ⋮ → Vlastní repozitáře → `https://github.com/AT622CZ/ha-2n-easygate`, typ *Integrace*.

Integrace `easygate` komunikuje AT příkazy s modulem Siemens MC39i/MC55 uvnitř
brány. Nastavení modulu nemění: SMS jdou v PDU režimu (umí i češtinu) a příkazy,
které manuál zakazuje (`AT+IPR=`, `AT+CFUN=`, `AT+CSCS=`, …), integrace odmítne.

| Entita / služba | Význam |
|---|---|
| `notify.2n_easygate_sms` | `notify.send_message` → SMS výchozím příjemcům |
| služba `easygate.send_sms` | SMS na zadané číslo (`force: true` obejde denní limit) |
| událost `easygate_sms` | přijatá SMS (`number`, `text`, `timestamp`); ze SIM se pak smaže |
| událost `easygate_call` | změna hovoru (`direction`, `state`, `number`) |
| událost `easygate_missed_call` | nový zmeškaný hovor (`number`), zjišťuje se z modulu jednou za minutu |
| `sensor` Signál GSM, Operátor, Registrace v síti | stav sítě |
| `sensor` Hovor GSM | klid / vytáčení / příchozí – hovor …, atribut `number` |
| `sensor` Poslední přijatá SMS, Poslední zmeškaný hovor | |
| `sensor` SMS odeslané dnes | počítadlo, atribut `daily_limit` |
| služba `easygate.at_command` | surový AT příkaz (pro ladění) |

**Pojistky proti útratě:** denní limit (výchozí 5 SMS), výchozí 1 SMS na zprávu
(delší text se zkrátí), odstranění diakritiky (160 místo 70 znaků). Nastavuje se
v možnostech integrace.

## Zapojení

* Brána je zapojená jako modem (DCE): **rovný kabel 1:1**, žádný nulový modem.
* **Nutné řízení toku RTS/CTS.** Převodník musí mít zapojené všechny piny DB-9.
  Při vytáčení a vyzvánění brána port blokuje (vypne CTS). Integrace to
  zobrazí jako „vytáčení / vyzvánění“ a odeslání SMS zopakuje, až se port uvolní.
* Když není brána registrovaná v síti GSM, port je zablokovaný trvale.
* Rychlost rozpozná brána sama, výchozí je 9600 Bd, 8N1.
* Se dvěma převodníky (M-LINE + EasyGate): levné CH340/PL2303 nemají sériové
  číslo a v `/dev/serial/by-id/` se nerozliší. Použijte `/dev/serial/by-path/…`
  (podle USB portu) nebo aspoň jeden převodník s čipem FTDI.

## Test v terminálu

```bash
python3 /config/custom_components/easygate/tools/easygate_cli.py /dev/ttyUSB1 info
```

```bash
python3 /config/custom_components/easygate/tools/easygate_cli.py /dev/ttyUSB1 monitor
```

`monitor` vypisuje hovory (`AT+CLCC`), nevyžádané hlášky modulu (RING, +CLIP)
a nové SMS. Během příchozího hovoru, po zvednutí a po zavěšení je vidět, co
brána na port pustí.

## Příklad: SMS při výpadku internetu

```yaml
automation:
  - alias: "SMS – výpadek internetu"
    triggers:
      - trigger: state
        entity_id: binary_sensor.internet   # např. integrace Ping na 8.8.8.8
        to: "off"
        for: "00:10:00"
    conditions:
      - condition: template   # nejvýš jedna SMS za 6 hodin
        value_template: >
          {{ this.attributes.last_triggered is none or
             now() - this.attributes.last_triggered > timedelta(hours=6) }}
    actions:
      - action: notify.send_message
        target:
          entity_id: notify.2n_easygate_sms
        data:
          message: "Doma nejde internet od {{ now().strftime('%H:%M') }}"

  - alias: "Příchozí SMS do mobilu"
    triggers:
      - trigger: event
        event_type: easygate_sms
    actions:
      - action: notify.mobile_app_telefon
        data:
          title: "SMS od {{ trigger.event.data.number }}"
          message: "{{ trigger.event.data.text }}"
```

## Ovládání HA přes SMS

V nastavení integrace EasyGate vyplňte **čísla, ze kterých SMS příkazy platí**, a
doporučeně i **PIN**. SMS z povoleného čísla ve tvaru `PIN PŘÍKAZ parametry`
vyvolá událost `easygate_command` (`number`, `command` velkými písmeny bez
diakritiky, `args`, `text`). SMS bez PINu nebo z cizího čísla se ignoruje.
Odesílatele SMS jde podvrhnout, proto PIN.

```yaml
automation:
  - alias: "SMS: topení zap/vyp"
    triggers:
      - trigger: event
        event_type: easygate_command
        event_data:
          command: TOPENI
    actions:
      - action: >
          {{ "climate.turn_on" if trigger.event.data.args[0] | upper == "ZAP"
             else "climate.turn_off" }}
        target:
          entity_id: climate.chata
```

Odpověď SMS (`easygate.send_sms`) stojí peníze a počítá se do denního limitu.

## Hovory přes EasyGate

* `easygate_call` se `state: ringing` přijde hned při zazvonění (bez čísla –
  brána ho na port nepustí, ověřeno s `AT+CLIP=1`).
* Po skončení vyzvánění integrace porovná seznamy přijatých (`RC`) a
  zmeškaných (`MC`) hovorů: přijatý → `easygate_call` se `state: answered`
  a číslem, jinak `easygate_missed_call` s číslem.
* Když si brána port jen na chvíli vezme pro sebe (údržba sítě), není to hlášeno
  jako hovor; vidět je to v atributu `port_blocked` senzoru Hovor GSM.

## Ověřeno na bráně (MC55i, rev. 01.003, Vodafone CZ)

* AT příkazy fungují na 9600 Bd s RTS/CTS; operátor se hlásí číselně (23003 → Vodafone CZ).
* Během vyzvánění modul na port nic neposílá kromě (často useknutého) `RING`;
  `+CLIP` je v modulu vypnutý (`AT+CLIP?` → `0,1`). Integrace proto bere
  číslo volajícího ze seznamu zmeškaných hovorů `MC` hned po skončení vyzvánění.
* SMS jsou ve dvou pamětech: `ME` (25) a SIM `SM` (20). Když jsou plné, brána
  další SMS nepřijme. Integrace čte obě, každou SMS připíše do
  `/config/easygate_sms.txt` a pak ji smaže. Zprávy, které na SIM byly před
  prvním spuštěním, se jen archivují, jako nové se nehlásí.

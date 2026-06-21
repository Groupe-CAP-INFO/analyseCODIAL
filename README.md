# analyseCODIAL

Analyse du système CODIAL.

## Description

Ce projet contient les scripts et outils pour analyser le système CODIAL.

## Installation

```bash
pip install -r requirements.txt
```

## Utilisation

1. Installez les dépendances Python :

```bash
pip install -r requirements.txt
```

2. Testez la connexion ODBC vers HFSQL :

```bash
python hfsql_connection_test.py --dsn TEST-HFSQL-CODIAL --debug
```

3. Si vous avez besoin de spécifier un driver explicite :

```bash
python hfsql_connection_test.py --driver-path "C:\\Program Files\\Common Files\\PC SOFT\\2024\\ODBC\\Win64x86\\wd290hfo64.dll" --server 192.168.0.4 --port 4900 --database CAPINFO --user admin --password "5f$ts4w!"
```

4. Lancez le scan principal :

```bash
python scan_hfsql.py
```

## Auteur

Groupe CAP INFO

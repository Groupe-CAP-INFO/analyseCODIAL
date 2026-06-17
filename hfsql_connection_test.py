import argparse
import pyodbc
import threading
import time


def test_connection(conn_str, timeout=15):
    result = {'status': None, 'error': None}

    def target():
        try:
            conn = pyodbc.connect(conn_str, autocommit=True)
            conn.close()
            result['status'] = 'connected'
        except Exception as exc:
            result['status'] = 'error'
            result['error'] = str(exc)

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(timeout=timeout)

    if thread.is_alive():
        return 'timeout', None
    return result['status'], result['error']


def build_connection_string(args):
    if args.dsn:
        conn_str = f"DSN={args.dsn};"
        if args.user:
            conn_str += f"UID={args.user};"
        if args.password:
            conn_str += f"PWD={args.password};"
        return conn_str

    if not args.driver or not args.server or not args.database:
        raise ValueError('Driver, server and database are required when DSN is not used.')

    conn_str = f"Driver={{{args.driver}}};Server Name={args.server}"
    if args.port:
        conn_str += f":{args.port}"
    conn_str += f";Database={args.database};"
    if args.user:
        conn_str += f"Uid={args.user};"
    if args.password:
        conn_str += f"Pwd={args.password};"
    return conn_str


def main():
    parser = argparse.ArgumentParser(description='Test HFSQL ODBC connection.')
    parser.add_argument('--dsn', help='ODBC DSN name to use')
    parser.add_argument('--driver', default='HFSQL', help='ODBC driver name to use')
    parser.add_argument('--server', default='192.168.0.4', help='HFSQL server host or address')
    parser.add_argument('--port', default='4900', help='HFSQL server port')
    parser.add_argument('--database', default='CAPINFO', help='HFSQL database name')
    parser.add_argument('--user', default='admin', help='HFSQL user name')
    parser.add_argument('--password', default='5f$ts4w!', help='HFSQL user password')
    parser.add_argument('--timeout', type=int, default=15, help='Seconds to wait before considering the connection blocked')
    args = parser.parse_args()

    try:
        conn_str = build_connection_string(args)
    except ValueError as exc:
        print('ERROR:', exc)
        return

    print('Testing HFSQL connection...')
    print('Connection string:')
    print(conn_str)
    print('\nWaiting up to', args.timeout, 'seconds...')

    status, error = test_connection(conn_str, timeout=args.timeout)

    if status == 'connected':
        print('\nRESULT: CONNECTED')
    elif status == 'error':
        print('\nRESULT: ERROR')
        print(error)
    else:
        print('\nRESULT: TIMEOUT')
        print('The driver did not return within the configured timeout.')


if __name__ == '__main__':
    main()

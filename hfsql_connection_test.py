import argparse
import pyodbc
import threading
import sys


def print_debug_info():
    print('Python executable:', sys.executable)
    print('Python version:', sys.version)
    print('ODBC drivers visible to pyodbc:')
    for driver in pyodbc.drivers():
        print(' -', driver)
    print('\nODBC data sources visible to pyodbc:')
    for name, driver in pyodbc.dataSources().items():
        print(' -', name, '=>', driver)
    print()


def test_connection(conn_str, timeout=15):
    result = {'status': None, 'error': None}

    def target():
        try:
            conn = pyodbc.connect(conn_str, autocommit=True)
            conn.close()
            result['status'] = 'connected'
        except Exception as exc:
            result['status'] = 'error'
            result['error'] = repr(exc)
            if hasattr(exc, 'args'):
                result['error_args'] = exc.args

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(timeout=timeout)

    if thread.is_alive():
        return {'status': 'timeout', 'error': None}
    return result


def build_connection_string(args):
    if args.dsn:
        conn_str = f"DSN={args.dsn};"
        if args.user:
            conn_str += f"UID={args.user};"
        if args.password:
            conn_str += f"PWD={args.password};"
        if args.timeout is not None:
            conn_str += f"Connection Timeout={args.timeout};"
        return conn_str

    driver_value = args.driver_path if args.driver_path else args.driver
    conn_str = f"Driver={{{driver_value}}};"
    server_value = args.server
    if args.port and ':' not in server_value:
        server_value = f"{server_value}:{args.port}"
    conn_str += f"Server Name={server_value};"
    conn_str += f"Database={args.database};"
    if args.user:
        conn_str += f"Uid={args.user};"
    if args.password:
        conn_str += f"Pwd={args.password};"
    if args.timeout is not None:
        conn_str += f"Connection Timeout={args.timeout};"
    return conn_str


def main():
    parser = argparse.ArgumentParser(description='Test HFSQL ODBC connection.')
    parser.add_argument('--dsn', help='ODBC DSN name to use')
    parser.add_argument('--driver', default='HFSQL', help='ODBC driver name to use')
    parser.add_argument('--driver-path', help='Full path to an ODBC driver DLL')
    parser.add_argument('--server', default='192.168.0.4', help='HFSQL server host or address')
    parser.add_argument('--port', default='4900', help='HFSQL server port')
    parser.add_argument('--database', default='CAPINFO', help='HFSQL database name')
    parser.add_argument('--user', default='admin', help='HFSQL user name')
    parser.add_argument('--password', default='5f$ts4w!', help='HFSQL user password')
    parser.add_argument('--timeout', type=int, default=15, help='Seconds to wait before considering the connection blocked')
    parser.add_argument('--debug', action='store_true', help='Print pyodbc drivers and data sources')
    args = parser.parse_args()

    if args.debug:
        print_debug_info()

    try:
        conn_str = build_connection_string(args)
    except ValueError as exc:
        print('ERROR:', exc)
        return

    print('Testing HFSQL connection...')
    print('Connection string:')
    print(conn_str)
    print('\nWaiting up to', args.timeout, 'seconds...')

    result = test_connection(conn_str, timeout=args.timeout)

    if result['status'] == 'connected':
        print('\nRESULT: CONNECTED')
    elif result['status'] == 'error':
        print('\nRESULT: ERROR')
        print(result['error'])
        if 'error_args' in result:
            print('ARGS:', result['error_args'])
    else:
        print('\nRESULT: TIMEOUT')
        print('The driver did not return within the configured timeout.')


if __name__ == '__main__':
    main()

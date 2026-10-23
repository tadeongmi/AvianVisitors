<?php
require_once dirname(__DIR__) . '/avian/api/admin-auth.php';

function delete_recording_request(string $selection): void {
    ensure_authenticated('You must be authenticated to delete recordings.');
    avian_require_json_action();
    $raw = file_get_contents('php://input', false, null, 0, 4097);
    if (!is_string($raw) || strlen($raw) > 4096) avian_api_fail(413, 'Request is too large');
    $body = json_decode($raw, true);
    if (!is_array($body) || array_keys($body) !== [$selection]
        || !is_string($body[$selection]) || $body[$selection] === ''
        || strlen($body[$selection]) > 1024 || str_contains($body[$selection], "\0")) {
        avian_api_fail(400, 'Invalid recording selection');
    }
    $pipes = [];
    $process = proc_open(
        ['/usr/bin/sudo', '-n', '/usr/local/sbin/avian-admin-control', 'delete-recordings'],
        [0 => ['pipe', 'r'], 1 => ['pipe', 'w'], 2 => ['file', '/dev/null', 'a']],
        $pipes
    );
    if (!is_resource($process)) avian_api_fail(503, 'Recording deletion is unavailable');
    fwrite($pipes[0], json_encode($body));
    fclose($pipes[0]);
    $output = stream_get_contents($pipes[1]);
    fclose($pipes[1]);
    $status = proc_close($process);
    $result = json_decode($output, true);
    if (!is_array($result)) avian_api_fail(503, 'Recording deletion is unavailable; update services first');
    if ($status !== 0 || ($result['ok'] ?? false) !== true) {
        avian_api_fail(409, (string)($result['error'] ?? 'Recording deletion failed'));
    }
    header('Content-Type: application/json; charset=utf-8');
    header('Cache-Control: no-store');
    echo json_encode($result);
    exit;
}

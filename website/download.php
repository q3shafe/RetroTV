<?php
$log_file = __DIR__ . '/downloads.log';

$date_time = date('Y-m-d H:i:s');
$ip        = $_SERVER['HTTP_X_FORWARDED_FOR'] ?? $_SERVER['REMOTE_ADDR'] ?? 'unknown';
$browser   = $_SERVER['HTTP_USER_AGENT'] ?? 'unknown';

$platform  = (isset($_GET['p']) && in_array($_GET['p'], ['win', 'linux'], true)) ? $_GET['p'] : 'win';
$entry = "[{$date_time}] [{$platform}] IP: {$ip} | UA: {$browser}" . PHP_EOL;

file_put_contents($log_file, $entry, FILE_APPEND | LOCK_EX);

header('Location: https://github.com/q3shafe/RetroTV/releases/tag/v2.2.2', true, 302);
exit;

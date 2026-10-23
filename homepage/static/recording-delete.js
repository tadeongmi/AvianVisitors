async function deleteRecordingRequest(url, selection) {
  const response = await fetch(url, {
    method: 'POST',
    credentials: 'same-origin',
    headers: {'Content-Type': 'application/json', 'X-Avian-Action': '1'},
    body: JSON.stringify(selection)
  });
  let result;
  try { result = await response.json(); }
  catch { throw new Error('Could not delete recordings. Reload the page and try again.'); }
  if (!response.ok || result?.ok !== true) {
    throw new Error(result?.error || 'Could not delete recordings.');
  }
  if (result.warning) alert(result.warning);
  return result;
}

async function deleteDetection(filename, copylink = false) {
  if (!confirm('Delete this detection and its local recording?')) return;
  try {
    await deleteRecordingRequest('play.php?deletefile=1', {file: filename});
    if (copylink) window.top.close();
    else location.reload();
  } catch (error) {
    alert(error.message);
  }
}

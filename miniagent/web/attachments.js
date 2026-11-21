'use strict';

window.studioMessageFiles = (() => {
 // Drafts outlive their DOM nodes while a room is polling or the user visits another chat.
 const drafts = new Map();
 const locked = new Set();
 const entries = key => drafts.get(key) || [];
 const keyFor = form => form.querySelector('[data-attachment-key]')?.dataset.attachmentKey;
 const count = key => entries(key).length;
 const downloadURL = (run,id) => '/api/runs/'+encodeURIComponent(run)+'/attachments/'+encodeURIComponent(id);

 function fileLabel(name, size) {
  const bytes = size >= 1048576 ? `${(size / 1048576).toFixed(1)} MB` : sizeText(size);
  return `${icon('file')}<span class="upload-name" dir="auto">${e(name)}</span><small>${e(bytes)}</small>`;
 }

 function chips(key) {
  return entries(key).map(item => {
   const name = item.file.name;
   const status = item.error ? `<span class="upload-error" role="alert">${e(item.error)}</span>`
    : item.uploading ? '<small role="status">上传中…</small>' : item.upload ? '<small>已上传</small>' : '';
   return `<li class="upload-chip" data-key="file-${e(item.key)}">
    ${fileLabel(name,item.file.size)}${status}
    <button type="button" data-remove-upload="${e(item.key)}" title="移除附件" aria-label="移除 ${e(name)}">${icon('x')}</button></li>`;
  }).join('');
 }

 function picker(key, disabled=false) {
  if (!Number.isInteger(state.config?.max_message_attachments)) return '';
  return `<fieldset class="upload-picker" data-attachment-key="${e(key)}" aria-label="消息附件" ${disabled || locked.has(key)?'disabled':''}>
    <input type="file" multiple hidden data-upload-input aria-label="选择要随消息发送的文件">
    <button type="button" class="upload-add" data-pick-uploads>${icon('paperclip')}<span>添加附件</span></button>
    <ul class="upload-list">${chips(key)}</ul></fieldset>`;
 }

 function redraw(key) {
  for (const picker of document.querySelectorAll('[data-attachment-key]')) {
   if (picker.dataset.attachmentKey===key) patchHTML(picker.querySelector('.upload-list'),chips(key));
  }
 }

 function hold(key, value) {
  if (!key) return;
  if (value) locked.add(key); else locked.delete(key);
  for (const picker of document.querySelectorAll('[data-attachment-key]')) {
   if (picker.dataset.attachmentKey===key) picker.disabled=value;
  }
 }

 async function prepare(key) {
  const uploaded=[];
  for (const item of entries(key)) {
   if (!item.upload) {
    item.error=''; item.uploading=true; redraw(key);
    try {
     const {upload} = await api('/api/uploads?name='+encodeURIComponent(item.file.name), {
      method:'POST',headers:{'Content-Type':'application/octet-stream'},body:item.file
     });
     if (!upload?.id) throw new Error('服务未返回附件编号。');
     item.upload=upload;
    } catch (error) {
     item.error=error.message;
     const failure=new Error(`附件「${item.file.name}」上传失败，重新发送即可重试。`);
     failure.code=error.code;
     throw failure;
    } finally {
     item.uploading=false; redraw(key);
    }
   }
   uploaded.push(item.upload);
  }
  return uploaded;
 }

 function history(turn) {
  const items = turn.message_attachments || [];
  if (!items.length) return '';
  return `<ul class="upload-list sent-uploads" aria-label="随消息发送的附件">${items.map(item=>{
   const url=downloadURL(turn.run_id,item.id);
   if (StudioMarkdown.canPreviewAttachment(item.name,item.size)) {
    return `<li class="upload-chip previewable-upload" data-key="sent-${e(item.id)}">
     <button type="button" data-preview-upload="${e(item.id)}" data-upload-run="${e(turn.run_id)}" data-upload-name="${e(item.name)}" data-upload-size="${e(item.size)}"
      aria-label="预览 ${e(item.name)}" title="预览 Markdown">${fileLabel(item.name,item.size)}</button>
     <a href="${e(url)}" download="${e(item.name)}" aria-label="下载 ${e(item.name)}" title="下载原件">${icon('download')}</a></li>`;
   }
   return `<li data-key="sent-${e(item.id)}"><a class="upload-chip" href="${e(url)}" download="${e(item.name)}" aria-label="下载 ${e(item.name)}">
    ${fileLabel(item.name,item.size)}${icon('download')}</a></li>`;
  }).join('')}</ul>`;
 }

 function changed(picker) {
  redraw(picker.dataset.attachmentKey);
  picker.dispatchEvent(new Event('attachmentschange',{bubbles:true}));
 }
 document.addEventListener('change',event=>{
  const input=event.target;
  if (!input.matches('[data-upload-input]')) return;
  const picker=input.closest('[data-attachment-key]');
  const key=picker.dataset.attachmentKey;
  if (!picker.disabled && input.files.length) {
   if (entries(key).length + input.files.length > state.config?.max_message_attachments) {
    toast(`每条消息最多可以添加 ${state.config.max_message_attachments} 个文件。`);
    input.value='';
    return;
   }
   const selected=Array.from(input.files,file=>({key:crypto.randomUUID(),file}));
   drafts.set(key,[...entries(key),...selected]);
   changed(picker);
  }
  input.value='';
 });
 document.addEventListener('click',event=>{
  const preview=event.target.closest('[data-preview-upload]');
  if (preview) {
   const {uploadName,uploadSize,uploadRun,previewUpload}=preview.dataset;
   StudioMarkdown.openAttachment(uploadName,Number(uploadSize),downloadURL(uploadRun,previewUpload));
   return;
  }
  const button=event.target.closest('[data-pick-uploads],[data-remove-upload]');
  if (!button || button.matches(':disabled')) return;
  const picker=button.closest('[data-attachment-key]');
  if (button.hasAttribute('data-pick-uploads')) {
   picker.querySelector('[data-upload-input]').click();
   return;
  }
  const key=picker.dataset.attachmentKey;
  const rest=entries(key).filter(item=>item.key!==button.dataset.removeUpload);
  if (rest.length) drafts.set(key,rest); else drafts.delete(key);
  changed(picker);
 });
 return {picker,keyFor,count,hold,prepare,history,clear:key=>drafts.delete(key)};
})();

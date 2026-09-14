// 主JavaScript文件
document.addEventListener('DOMContentLoaded', function() {
    console.log('Y2A-Auto 已加载');

    // --- 设置页面的密码保护逻辑 ---
    const settingsForm = document.querySelector('form[method="post"][enctype="multipart/form-data"]');
    if (settingsForm) {
        const newPassword = document.getElementById('new_password');
        const confirmPassword = document.getElementById('confirm_password');
        const passwordError = document.getElementById('password-match-error');
        const passwordProtectionEnabled = document.getElementById('password_protection_enabled');
        const passwordFields = document.getElementById('password-fields');

        if (passwordProtectionEnabled) {
            function togglePasswordFields() {
                if (passwordProtectionEnabled.checked) {
                    passwordFields.style.display = 'block';
                } else {
                    passwordFields.style.display = 'none';
                }
            }

            // Initial state
            togglePasswordFields();
            passwordProtectionEnabled.addEventListener('change', togglePasswordFields);
        }

        settingsForm.addEventListener('submit', function(event) {
            // 仅在启用密码保护时才校验密码匹配
            if (passwordProtectionEnabled && passwordProtectionEnabled.checked &&
                newPassword && confirmPassword && newPassword.value !== confirmPassword.value) {
                event.preventDefault(); // 阻止表单提交
                if (passwordError) {
                    passwordError.classList.remove('d-none');
                }
            } else {
                if (passwordError) {
                    passwordError.classList.add('d-none');
                }
            }
        });
    }


    // --- 设置页面的日志清理按钮逻辑 ---
    // 绑定手动日志清理按钮
    const manualCleanupBtn = document.getElementById('manual-cleanup-btn');
    const logCleanupHoursField = document.getElementById('log-cleanup-hours');
    const cleanupHoursHidden = document.getElementById('cleanup-hours-input');
    if(manualCleanupBtn) {
        manualCleanupBtn.addEventListener('click', function() {
            // 使用当前输入的小时数（若存在）
            if (logCleanupHoursField && cleanupHoursHidden) {
                const hours = parseInt(logCleanupHoursField.value, 10);
                if (!isNaN(hours) && hours > 0) {
                    cleanupHoursHidden.value = hours;
                }
            }
            const confirmMsg = `确定要手动清理旧日志吗？将删除 ${cleanupHoursHidden ? cleanupHoursHidden.value : ''} 小时前的日志文件。`;
            if (confirm(confirmMsg)) {
                const form = document.getElementById('cleanup-form');
                if (form) form.submit();
            }
        });
    }

    // 绑定立即清空日志按钮
    const clearLogsBtn = document.getElementById('clear-logs-btn');
    const confirmClearBtn = document.getElementById('confirm-clear-btn');
    const cancelClearBtn = document.getElementById('cancel-clear-btn');
    const clearWarning = document.getElementById('clear-warning');

    if (clearLogsBtn) {
        clearLogsBtn.addEventListener('click', function() {
            clearLogsBtn.classList.add('d-none');
            confirmClearBtn.classList.remove('d-none');
            cancelClearBtn.classList.remove('d-none');
            clearWarning.classList.remove('d-none');
        });
    }

    if (cancelClearBtn) {
        cancelClearBtn.addEventListener('click', function() {
            clearLogsBtn.classList.remove('d-none');
            confirmClearBtn.classList.add('d-none');
            cancelClearBtn.classList.add('d-none');
            clearWarning.classList.add('d-none');
        });
    }
    
    if (confirmClearBtn) {
        confirmClearBtn.addEventListener('click', function() {
             document.getElementById('clear-form').submit();
        });
    }

    // --- 设置页面的下载内容清理按钮逻辑 ---
    // 绑定手动下载内容清理按钮
    const manualDownloadCleanupBtn = document.getElementById('manual-download-cleanup-btn');
    const downloadCleanupHoursField = document.getElementById('download-cleanup-hours');
    const downloadCleanupHoursHidden = document.getElementById('download-cleanup-hours-input');
    if(manualDownloadCleanupBtn) {
        manualDownloadCleanupBtn.addEventListener('click', function() {
            // 使用当前输入的小时数（若存在）
            if (downloadCleanupHoursField && downloadCleanupHoursHidden) {
                const hours = parseInt(downloadCleanupHoursField.value, 10);
                if (!isNaN(hours) && hours > 0) {
                    downloadCleanupHoursHidden.value = hours;
                }
            }
            const confirmMsg = `确定要手动清理旧的下载内容吗？将删除 ${downloadCleanupHoursHidden ? downloadCleanupHoursHidden.value : ''} 小时前的下载文件和目录。`;
            if (confirm(confirmMsg)) {
                const form = document.getElementById('download-cleanup-form');
                if (form) form.submit();
            }
        });
    }
});

// ==========================================================================
// 全局 Popconfirm 气泡确认框组件
// ==========================================================================
let _activePopconfirm = null;

window.closeActivePopconfirm = function() {
    if (_activePopconfirm && _activePopconfirm.bubble) {
        if (_activePopconfirm.bubble.parentNode) {
            _activePopconfirm.bubble.parentNode.removeChild(_activePopconfirm.bubble);
        }
        if (typeof _activePopconfirm.onClose === 'function') {
            _activePopconfirm.onClose();
        }
        _activePopconfirm = null;
    }
};

document.addEventListener('click', function(e) {
    if (_activePopconfirm && _activePopconfirm.bubble) {
        if (!_activePopconfirm.bubble.contains(e.target) && !_activePopconfirm.trigger.contains(e.target)) {
            window.closeActivePopconfirm();
        }
    }
});

document.addEventListener('keydown', function(e) {
    if (e.key === 'Escape') {
        window.closeActivePopconfirm();
    }
});

window.addEventListener('scroll', function() {
    if (_activePopconfirm) {
        window.closeActivePopconfirm();
    }
}, true);

window.showPopconfirm = function(triggerEl, options) {
    if (!triggerEl) return;

    // 如果点击的是当前已打开的气泡触发按钮，则关闭它
    if (_activePopconfirm && _activePopconfirm.trigger === triggerEl) {
        window.closeActivePopconfirm();
        return;
    }
    window.closeActivePopconfirm();

    options = options || {};
    const title = options.title || '确定执行此操作吗？';
    const description = options.description || '';
    const okText = options.okText || '确定';
    const cancelText = options.cancelText || '取消';
    const okClass = options.okClass || 'btn-success';
    const iconClass = options.iconClass || 'bi-question-circle-fill text-warning';

    const bubble = document.createElement('div');
    bubble.className = 'popconfirm-bubble';
    bubble.innerHTML = `
        <div class="popconfirm-content">
            <div class="popconfirm-message">
                <i class="bi ${iconClass}"></i>
                <div class="popconfirm-text">
                    <div class="popconfirm-title">${title}</div>
                    ${description ? `<div class="popconfirm-desc">${description}</div>` : ''}
                </div>
            </div>
            <div class="popconfirm-actions">
                <button type="button" class="btn btn-light btn-sm popconfirm-cancel-btn">${cancelText}</button>
                <button type="button" class="btn ${okClass} btn-sm popconfirm-ok-btn">${okText}</button>
            </div>
        </div>
        <div class="popconfirm-arrow"></div>
    `;

    document.body.appendChild(bubble);

    const cancelBtn = bubble.querySelector('.popconfirm-cancel-btn');
    const okBtn = bubble.querySelector('.popconfirm-ok-btn');

    cancelBtn.addEventListener('click', function(e) {
        e.stopPropagation();
        window.closeActivePopconfirm();
    });

    okBtn.addEventListener('click', function(e) {
        e.stopPropagation();
        if (typeof options.onConfirm === 'function') {
            const originalHtml = okBtn.innerHTML;
            okBtn.disabled = true;
            okBtn.innerHTML = '<span class="spinner-border spinner-border-sm" role="status" aria-hidden="true"></span>';
            cancelBtn.disabled = true;

            Promise.resolve(options.onConfirm(okBtn, cancelBtn))
                .then(function(shouldClose) {
                    if (shouldClose !== false) {
                        window.closeActivePopconfirm();
                    } else {
                        okBtn.disabled = false;
                        okBtn.innerHTML = originalHtml;
                        cancelBtn.disabled = false;
                    }
                })
                .catch(function(err) {
                    console.error('Popconfirm action error:', err);
                    okBtn.disabled = false;
                    okBtn.innerHTML = originalHtml;
                    cancelBtn.disabled = false;
                });
        } else {
            window.closeActivePopconfirm();
        }
    });

    // 计算定位
    const triggerRect = triggerEl.getBoundingClientRect();
    const bubbleRect = bubble.getBoundingClientRect();
    const arrow = bubble.querySelector('.popconfirm-arrow');

    const margin = 8;
    const placeTop = triggerRect.top >= bubbleRect.height + margin + 12;

    const top = placeTop 
        ? triggerRect.top - bubbleRect.height - margin 
        : triggerRect.bottom + margin;

    let left = triggerRect.left + (triggerRect.width / 2) - (bubbleRect.width / 2);

    // 视口边缘防溢出
    const minLeft = 12;
    const maxLeft = window.innerWidth - bubbleRect.width - 12;
    if (left < minLeft) left = minLeft;
    if (left > maxLeft) left = maxLeft;

    bubble.style.top = `${top}px`;
    bubble.style.left = `${left}px`;

    if (placeTop) {
        bubble.classList.add('popconfirm-placement-top');
    } else {
        bubble.classList.add('popconfirm-placement-bottom');
    }

    if (arrow) {
        const arrowLeft = triggerRect.left + (triggerRect.width / 2) - left - 5;
        arrow.style.left = `${Math.max(12, Math.min(arrowLeft, bubbleRect.width - 22))}px`;
    }

    _activePopconfirm = {
        trigger: triggerEl,
        bubble: bubble,
        onClose: options.onClose
    };
};
 
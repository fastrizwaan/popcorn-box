pkgdatadir = join_paths(get_option('prefix'), get_option('datadir'), meson.project_name())
moduledir = join_paths(pkgdatadir, 'popcorn_box')

install_data(
    'popcorn_box/__init__.py',
    'popcorn_box/api.py',
    'popcorn_box/database.py',
    'popcorn_box/main.py',
    'popcorn_box/player.py',
    'popcorn_box/player_widget.py',
    'popcorn_box/window.py',
    'popcorn_box/libtorrent_stream.py',
    install_dir: moduledir
)

conf = configuration_data()
conf.set('pkgdatadir', pkgdatadir)
conf.set('localedir', join_paths(get_option('prefix'), get_option('localedir')))

configure_file(
    input: 'popcorn-box.in',
    output: 'popcorn-box',
    configuration: conf,
    install: true,
    install_dir: get_option('bindir'),
    install_mode: 'rwxr-xr-x'
)

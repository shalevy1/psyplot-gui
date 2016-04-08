"""Help explorer widget supplying a simple web browser and a plain text help
viewer"""
import os.path as osp
import traceback as tb
import sys
import warnings
from collections import namedtuple
from itertools import chain
import re
import six
import types
import inspect
import shutil
from psyplot.docstring import dedents, indent, docstrings
from psyplot.compat.pycompat import OrderedDict
from psyplot.data import _TempBool
from psyplot_gui.compat.qtcompat import (
    QWidget, QHBoxLayout, QFrame, QVBoxLayout, QWebView, QToolButton,
    QIcon, QtCore, QDockWidget, QComboBox, Qt,  QSortFilterProxyModel,
    QCompleter, QStandardItemModel, QPlainTextEdit, QAction, QMenu, with_qt5,
    QErrorMessage)
from psyplot_gui.common import get_icon, DockMixin, PyErrorMessage
from IPython.core.oinspect import signature, getdoc
import logging
from psyplot_gui.common import get_module_path
from tempfile import mkdtemp
try:
    from sphinx.application import Sphinx
    from sphinx.util import get_module_source
    try:
        from psyplot.sphinxext.extended_napoleon import (
            ExtendedNumpyDocstring as NumpyDocstring,
            ExtendedGoogleDocstring as GoogleDocstring)
    except ImportError:
        from sphinx.ext.napoleon import NumpyDocstring, GoogleDocstring
    with_sphinx = True
except ImportError:
    with_sphinx = False


logger = logging.getLogger(__name__)


class UrlCombo(QComboBox):
    """A editable ComboBox with autocompletion"""

    def __init__(self, *args, **kwargs):
        super(UrlCombo, self).__init__(*args, **kwargs)
        self.setInsertPolicy(self.InsertAtTop)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setEditable(True)
        self.completer = QCompleter(self)

        # always show all completions
        self.completer.setCompletionMode(QCompleter.UnfilteredPopupCompletion)
        self.pFilterModel = QSortFilterProxyModel(self)
        self.pFilterModel.setFilterCaseSensitivity(Qt.CaseInsensitive)

        self.completer.setPopup(self.completer.popup())

        self.setCompleter(self.completer)

        self.lineEdit().textEdited[str].connect(
            self.pFilterModel.setFilterFixedString)
        self.completer.activated.connect(self.add_text_on_top)
        self.setModel(QStandardItemModel())

    def setModel(self, model):
        """Reimplemented to also set the model of the filter and completer"""
        super(UrlCombo, self).setModel(model)
        self.pFilterModel.setSourceModel(model)
        self.completer.setModel(self.pFilterModel)

    def add_text_on_top(self, text=None, block=False):
        """Add the given text as the first item"""
        if text is None:
            text = self.currentText()
        ind = self.findText(text)
        if block:
            self.blockSignals(True)
        if ind == -1:
            self.insertItem(0, text)
        elif ind != 0:
            self.removeItem(ind)
            self.insertItem(0, text)
        self.setCurrentIndex(0)
        if block:
            self.blockSignals(False)

    # replace keyPressEvent to always insert the selected item at the top
    def keyPressEvent(self, event):
        """Handle key press events"""
        if event.key() == Qt.Key_Return or event.key() == Qt.Key_Enter:
            self.add_text_on_top()
        else:
            QComboBox.keyPressEvent(self, event)



class UrlBrowser(QFrame):
    """Very simple browser with session history and autocompletion based upon
    the :class:`PyQt5.QtWebKitWidgets.QWebView` class

    Warnings
    --------
    This class is known to crash under PyQt4 when new web page domains are
    loaded. Hence it should be handled with care"""

    completed = _TempBool(True)

    url_like_re = re.compile('^\w+://')

    doc_urls = OrderedDict([
        ('startpage', 'https://startpage.com/'),
        ('psyplot', 'http://psyplot.readthedocs.org/en/latest/'),
        ('pyplot', 'http://matplotlib.org/api/pyplot_api.html'),
        ('seaborn', 'http://stanford.edu/~mwaskom/software/seaborn/api.html'),
        ('cartopy', 'http://scitools.org.uk/cartopy/docs/latest/index.html'),
        ('xarray', 'http://xarray.pydata.org/en/stable/'),
        ('pandas', 'http://pandas.pydata.org/pandas-docs/stable/'),
        ('numpy', 'https://docs.scipy.org/doc/numpy/reference/routines.html'),
        ])

    #: The initial url showed in the webview
    default_url = six.text_type("https://psyplot.readthedocs.org/en/latest/")

    def __init__(self, *args, **kwargs):
        super(UrlBrowser, self).__init__(*args, **kwargs)

        # ---------------------------------------------------------------------
        # ---------------------------- upper buttons --------------------------
        # ---------------------------------------------------------------------
        #: adress line
        self.tb_url = UrlCombo(self)
        #: button to go to previous url
        self.bt_back = QToolButton(self)
        #: button to go to next url
        self.bt_ahead = QToolButton(self)
        #: refresh the current url
        self.bt_refresh = QToolButton(self)
        #: button to go lock to the current url
        self.bt_lock = QToolButton(self)
        #: button to disable browsing in www
        self.bt_url_lock = QToolButton(self)

        # ---------------------------- buttons settings -----------------------
        self.bt_back.setIcon(QIcon(get_icon('previous.png')))
        self.bt_back.setToolTip('Go back one page')
        self.bt_ahead.setIcon(QIcon(get_icon('next.png')))
        self.bt_back.setToolTip('Go forward one page')

        self.bt_refresh.setIcon(QIcon(get_icon('refresh.png')))
        self.bt_refresh.setToolTip('Refresh the current page')

        self.bt_lock.setCheckable(True)
        self.bt_url_lock.setCheckable(True)

        if not with_qt5:
            # We now that the browser can crash with Qt4, therefore we disable
            # the browing in the internet
            self.bt_url_lock.click()

        self.bt_url_lock.clicked.connect(self.toogle_url_lock)
        self.bt_lock.clicked.connect(self.toogle_lock)

        # tooltip and icons of lock and url_lock are set in toogle_lock and
        # toogle_url_lock
        self.toogle_lock()
        self.toogle_url_lock()

        # ---------------------------------------------------------------------
        # --------- initialization and connection of the web view -------------
        # ---------------------------------------------------------------------

        #: The actual widget showing the html content
        self.html = QWebView(parent=self)
        self.html.loadStarted.connect(self.completed)
        self.html.loadFinished.connect(self.completed)

        self.tb_url.currentIndexChanged[str].connect(self.browse)
        self.bt_back.clicked.connect(self.html.back)
        self.bt_ahead.clicked.connect(self.html.forward)
        self.bt_refresh.clicked.connect(self.html.reload)
        self.html.urlChanged.connect(self.url_changed)

        # ---------------------------------------------------------------------
        # ---------------------------- layouts --------------------------------
        # ---------------------------------------------------------------------

        #: The upper part of the browser containing all the buttons
        self.button_box = button_box = QHBoxLayout()

        button_box.addWidget(self.bt_back)
        button_box.addWidget(self.bt_ahead)
        button_box.addWidget(self.tb_url)
        button_box.addWidget(self.bt_refresh)
        button_box.addWidget(self.bt_lock)
        button_box.addWidget(self.bt_url_lock)

        #: The upper most layout aranging the button box and the html widget
        self.vbox = vbox = QVBoxLayout()
        self.vbox.setContentsMargins(0, 0, 0, 0)
        vbox.addLayout(button_box)

        vbox.addWidget(self.html)

        self.setLayout(vbox)

        self.tb_url.addItem(self.default_url)

    def browse(self, url):
        """Make a web browse on the given url and show the page on the Webview
        widget. """
        if self.bt_lock.isChecked():
            return
        if not self.url_like_re.match(url):
            url = 'http://' + url
        if self.bt_url_lock.isChecked() and url.startswith('http'):
            return
        if not self.completed:
            logger.debug('Stopping current load...')
            self.html.stop()
            self.completed = True
        logger.debug('Loading %s', url)
        self.html.load(QtCore.QUrl(url))

    def url_changed(self, url):
        """Triggered when the url is changed to update the adress line"""
        try:
            url = url.toString()
        except AttributeError:
            pass
        logger.debug('url changed to %s', url)
        self.tb_url.setEditText(url)
        self.tb_url.add_text_on_top(url, block=True)

    def toogle_url_lock(self):
        """Disable (or enable) the loading of web pages in www"""
        bt = self.bt_url_lock
        bt.setIcon(QIcon(get_icon(
            'world_red.png' if bt.isChecked() else 'world.png')))
        online_message = "Go online"
        if not with_qt5:
            online_message += ("\nWARNING: This mode is unstable under Qt4 "
                               "and might result in a complete program crash!")
        bt.setToolTip(online_message if bt.isChecked() else
                      "Offline mode")

    def toogle_lock(self):
        """Disable (or enable) the changing of the current webpage"""
        bt = self.bt_lock
        bt.setIcon(QIcon(get_icon(
            'lock.png' if bt.isChecked() else 'lock_open.png')))
        bt.setToolTip("Unlock" if bt.isChecked() else "Lock to current page")


class HelpMixin(object):
    """Base class for providing help on an object"""

    #: Object containing the necessary fields to describe an object given to
    #: the help widget. The descriptor is set up by the :meth:`describe_object`
    #: method.
    object_descriptor = namedtuple('ObjectDescriptor', ['obj', 'name'])

    #: :class:`bool` determining whether the documentation of an object can be
    #: shown or not
    can_document_object = True
    #: :class:`bool` determining whether this class can show restructured text
    can_show_rst = True

    @docstrings.get_sectionsf('HelpMixin.show_help')
    def show_help(self, obj, oname=''):
        """
        Show the rst documentation for the given object

        Parameters
        ----------
        obj: object
            The object to get the documentation for
        oname: str
            The name to use for the object in the documentation"""
        descriptor = self.describe_object(obj, oname)
        doc = self.get_doc(descriptor)
        self.show_rst(doc, descriptor=descriptor)

    def header(self, descriptor, sig):
        """Format the header and include object name and signature `sig`

        Returns
        -------
        str
            The header for the documentation"""
        bars = '=' * len(descriptor.name + sig)
        return bars + '\n' + descriptor.name + sig + '\n' + bars + '\n'

    def describe_object(self, obj, oname=''):
        """Return an instance of the :attr:`object_descriptor` class

        Returns
        -------
        :attr:`object_descriptor`
            The descriptor containing the information on the object"""
        return self.object_descriptor(obj, oname)

    def get_doc(self, descriptor):
        """Get the documentation of the object in the given `descriptor`

        Parameters
        ----------
        descriptor: instance of :attr:`object_descriptor`
            The descriptor containig the information on the specific object

        Returns
        -------
        str
            The header and documentation of the object in the descriptor

        Notes
        -----
        This method uses the :func:`IPython.core.oinspect.getdoc` function to
        get the documentation and the :func:`IPython.core.oinspect.signature`
        function to get the signature. Those function (different from the
        inspect module) do not fail when the object is not saved"""
        obj = descriptor.obj
        oname = descriptor.name
        sig = ''
        obj_sig = obj
        if callable(obj):
            if inspect.isclass(obj):
                oname = oname or obj.__name__
                obj_sig = obj.__init__
            elif six.PY2 and type(obj) is types.InstanceType:
                obj_sig = obj.__call__

            try:
                sig = str(signature(obj_sig))
                sig = re.sub('^\(\s*self,\s*', '(', sig)
            except:
                logger.debug('Failed to get signature from %s!' % (obj, ),
                             exc_info=True)
        oname = oname or type(oname).__name__
        head = self.header(descriptor, sig)
        lines = []
        ds = getdoc(obj)
        if ds:
            lines.append('')
            lines.append(ds)
        if inspect.isclass(obj) and hasattr(obj, '__init__'):
            init_ds = getdoc(obj.__init__)
            if init_ds is not None:
                lines.append('\n' + init_ds)
        elif hasattr(obj, '__call__'):
            call_ds = getdoc(obj.__call__)
            if call_ds and call_ds != getdoc(object.__call__):
                lines.append('\n' + call_ds)
        doc = self.process_docstring(lines, descriptor)
        return head + '\n' + doc

    def process_docstring(self, lines, descriptor):
        """Make final modification on the rst lines

        Returns
        -------
        str
            The docstring"""
        return '\n'.join(lines)

    @docstrings.get_sectionsf('HelpMixin.show_rst')
    def show_rst(self, text, oname='', descriptor=None):
        """Abstract method which needs to be implemented by th widget to show
        restructured text

        Parameters
        ----------
        text: str
            The text to show
        oname: str
            The object name
        descriptor: instance of :attr:`object_descriptor`
            The object descriptor holding the informations"""
        pass


class TextHelp(QFrame, HelpMixin):
    """Class to show plain text rst docstrings"""

    def __init__(self, *args, **kwargs):
        super(TextHelp, self).__init__(*args, **kwargs)
        self.vbox = QVBoxLayout()
        self.vbox.setContentsMargins(0, 0, 0, 0)
        #: The :class:`PyQt5.QtWidgets.QPlainTextEdit` instance used for
        #: displaying the documentation
        self.editor = QPlainTextEdit(parent=self)
        self.vbox.addWidget(self.editor)
        self.setLayout(self.vbox)

    def show_rst(self, text, *args, **kwargs):
        """Show the given text in the editor window

        Parameters
        ----------
        text: str
            The text to show
        ``*args,**kwargs``
            Are ignored"""
        self.editor.clear()
        self.editor.insertPlainText(text)


class UrlHelp(UrlBrowser, HelpMixin):
    """Class to convert rst docstrings to html and show browsers"""

    #: Object containing the necessary fields to describe an object given to
    #: the help widget. The descriptor is set up by the :meth:`describe_object`
    #: method and contains an additional objtype attribute
    object_descriptor = namedtuple(
        'ObjectDescriptor', ['obj', 'name', 'objtype'])

    can_document_object = with_sphinx
    can_show_rst = with_sphinx

    def __init__(self, *args, **kwargs):
        self.sphinx_dir = kwargs.pop('sphinx_dir', mkdtemp())
        self.build_dir = osp.join(self.sphinx_dir, '_build', 'html')
        super(UrlHelp, self).__init__(*args, **kwargs)

        self.error_msg = PyErrorMessage(self)
        if with_sphinx:
            self.sphinx_thread = SphinxThread(self.sphinx_dir)
            self.sphinx_thread.html_ready[str].connect(self.browse)
            self.sphinx_thread.html_error[str].connect(
                self.error_msg.showTraceback)
            self.sphinx_thread.html_error[str].connect(logger.debug)
        else:
            self.sphinx_thread = None


        #: menu button with different urls
        self.bt_url_menus = QToolButton(self)
        self.bt_url_menus.setIcon(QIcon(get_icon('docu_button.png')))
        self.bt_url_menus.setToolTip('Browse documentations')
        self.bt_url_menus.setPopupMode(QToolButton.InstantPopup)

        docu_menu = QMenu(self)
        for name, url in six.iteritems(self.doc_urls):
            def to_url(b, url=url):
                self.browse(url)
            action = QAction(name, self)
            action.triggered.connect(to_url)
            docu_menu.addAction(action)
        self.bt_url_menus.setMenu(docu_menu)

        self.button_box.addWidget(self.bt_url_menus)

        if self.sphinx_thread is not None:
            self.sphinx_thread.render(None, None)

    def show_rst(self, text, oname='', descriptor=None):
        """Render restructured text with sphinx and show it

        Parameters
        ----------
        %(HelpMixin.show_rst.parameters)s"""
        if not oname and descriptor:
            oname = descriptor.name
        self.sphinx_thread.render(text, oname)

    def describe_object(self, obj, oname=''):
        """Describe an object using additionaly the object type from the
        :meth:`get_objtype` method

        Returns
        -------
        instance of :attr:`object_descriptor`
            The descriptor of the object"""
        return self.object_descriptor(obj, oname, self.get_objtype(obj))

    def browse(self, url):
        """Reimplemented to add file paths to the url string"""
        html_file = osp.join(self.sphinx_dir, '_build', 'html', url + '.html')
        if osp.exists(html_file):
            url = 'file://' + html_file
        super(UrlHelp, self).browse(url)

    def url_changed(self, url):
        """Reimplemented to remove file paths from the url string"""
        try:
            url = url.toString()
        except AttributeError:
            pass
        if url.startswith('file://'):
            fname = url[7:]
            if osp.samefile(self.build_dir, osp.commonprefix([
                    fname, self.build_dir])):
                url = osp.splitext(osp.basename(fname))[0]
        super(UrlHelp, self).url_changed(url)

    def header(self, descriptor, sig):
        return '%(name)s\n%(bars)s\n\n.. py:%(type)s:: %(name)s%(sig)s\n' % {
            'name': descriptor.name, 'bars': '-' * len(descriptor.name),
            'type': descriptor.objtype, 'sig': sig}

    def get_objtype(self, obj):
        """Get the object type of the given object and determine wheter the
        object is considered a class, a module, a function, method or data

        Parameters
        ----------
        obj: object

        Returns
        -------
        str
            One out of {'class', 'module', 'function', 'method', 'data'}"""
        if inspect.isclass(obj):
            return 'class'
        if inspect.ismodule(obj):
            return 'module'
        if inspect.isfunction(obj) or isinstance(obj, type(all)):
            return 'function'
        if inspect.ismethod(obj) or isinstance(obj, type(str.upper)):
            return 'method'
        return 'data'

    def is_importable(self, modname):
        """Determine whether members of the given module can be documented with
        sphinx by using the :func:`sphinx.util.get_module_source` function

        Parameters
        ----------
        modname: str
            The __name__ attribute of the module to import

        Returns
        -------
        bool
            True if sphinx can import the module"""
        try:
            get_module_source(modname)
            return True
        except:
            return False

    def get_doc(self, descriptor):
        """Reimplemented to (potentially) use the features from
        sphinx.ext.autodoc"""
        obj = descriptor.obj
        if inspect.ismodule(obj):
            module = obj
        else:
            module = inspect.getmodule(obj)
        if module is not None and (re.match('__.*__', module.__name__) or
                                   not self.is_importable(module.__name__)):
            module = None
        isclass = inspect.isclass(obj)
        # If the module is available, we try to use autodoc
        if module is not None:
            doc = '.. currentmodule:: ' + module.__name__ + '\n\n'
            # a module --> use automodule
            if inspect.ismodule(obj):
                doc += self.header(descriptor, '')
                doc += '.. automodule:: ' + obj.__name__
            # an importable class --> use autoclass
            elif isclass and getattr(module, obj.__name__, None) is not None:
                doc += self.header(descriptor, '')
                doc += '.. autoclass:: ' + obj.__name__
            # an instance and the class can be imported
            # --> use super get_doc and autoclass for the tyoe
            elif descriptor.objtype == 'data' and getattr(
                    module, type(obj).__name__, None) is not None:
                doc += '\n\n'.join([
                    super(UrlHelp, self).get_doc(descriptor),
                    "Class docstring\n===============",
                    '.. autoclass:: ' + type(obj).__name__])
            # an instance --> use super get_doc for instance and the type
            elif descriptor.objtype == 'data':
                cls_doc = super(UrlHelp, self).get_doc(self.describe_object(
                    type(obj), type(obj).__name__))
                doc += '\n\n'.join([
                    super(UrlHelp, self).get_doc(descriptor),
                    "Class docstring\n===============",
                    cls_doc])
            # a function or method --> use super get_doc
            else:
                doc += super(UrlHelp, self).get_doc(descriptor)
        # otherwise the object has been defined in this session
        else:
            # an instance --> use super get_doc for instance and the type
            if descriptor.objtype == 'data':
                cls_doc = super(UrlHelp, self).get_doc(self.describe_object(
                    type(obj), type(obj).__name__))
                doc = '\n\n'.join([
                    super(UrlHelp, self).get_doc(descriptor),
                    "Class docstring\n===============",
                    cls_doc])
            # a function or method --> use super get_doc
            else:
                doc = super(UrlHelp, self).get_doc(descriptor)
        return doc.rstrip() + '\n'

    def process_docstring(self, lines, descriptor):
        """Process the lines with the napoleon sphinx extension"""
        lines = list(chain(*(l.splitlines() for l in lines)))
        lines = NumpyDocstring(
            lines, what=descriptor.objtype, name=descriptor.name,
            obj=descriptor.obj).lines()
        lines = GoogleDocstring(
            lines, what=descriptor.objtype, name=descriptor.name,
            obj=descriptor.obj).lines()
        return indent(super(UrlHelp, self).process_docstring(
            lines, descriptor))


class StreamToLogger(object):
    """
    Fake file-like stream object that redirects writes to a logger instance.
    """
    def __init__(self, logger, log_level=logging.INFO):
        self.logger = logger
        self.log_level = log_level
        self.linebuf = ''

    def write(self, buf):
        for line in buf.rstrip().splitlines():
            self.logger.log(self.log_level, line.rstrip())


class SphinxThread(QtCore.QThread):
    """A thread to render sphinx documentation in a separate process"""

    #: A signal to be emitted when the rendering finished. The url is the
    #: file location
    html_ready = QtCore.pyqtSignal(str)
    html_error = QtCore.pyqtSignal(str)

    def __init__(self, outdir, html_text_no_doc=''):
        super(SphinxThread, self).__init__()
        self.doc = None
        self.name = None
        self.html_text_no_doc = html_text_no_doc
        self.outdir = outdir
        self.index_file = osp.join(self.outdir, 'psyplot.rst')
        self.confdir = osp.join(get_module_path(__name__), 'sphinx_supp')
        shutil.copyfile(osp.join(self.confdir, 'psyplot.rst'),
                        osp.join(self.outdir, 'psyplot.rst'))
        self.build_dir = osp.join(self.outdir, '_build', 'html')

    def render(self, doc, name):
        """Render the given rst string and save the file as ``name + '.rst'``

        Parameters
        ----------
        doc: str
            The rst docstring
        name: str
            the name to use for the file"""
        if self.wait():
            self.doc = doc
            self.name = name
            # start rendering in separate process
            self.start()

    def run(self):
        """Create the html file. When called the first time, it may take a
        while because the :class:`sphinx.application.Sphinx` app is build,
        potentially with intersphinx

        When finished, the html_ready signal is emitted"""
        if not hasattr(self, 'app'):
            from IPython.core.history import HistoryAccessor
            # to avoid history access conflicts between different threads,
            # we disable the ipython history
            HistoryAccessor.enabled.default_value = False
            self.app = Sphinx(self.outdir,
                              self.confdir,
                              self.build_dir,
                              osp.join(self.outdir, '_build', 'doctrees'),
                              'html',
                              status=StreamToLogger(logger, logging.DEBUG),
                              warning=StreamToLogger(logger, logging.DEBUG))
        if self.name is not None:
            docfile = osp.abspath(osp.join(self.outdir, self.name + '.rst'))
            if docfile == self.index_file:
                self.name += '1'
                docfile = osp.abspath(
                    osp.join(self.outdir, self.name + '.rst'))
            html_file = osp.abspath(osp.join(
                self.outdir, '_build', 'html', self.name + '.html'))
            if not osp.exists(docfile):
                with open(self.index_file, 'a') as f:
                    f.write('\n    ' + self.name)
            with open(docfile, 'w') as f:
                f.write(self.doc)
        else:
            html_file = osp.abspath(osp.join(
                self.outdir, '_build', 'html', 'psyplot.html'))
        try:
            self.app.build(None, [])
        except:
            msg = '<b>Error while building sphinx document %s</b>' % (
                self.name)
            self.html_error.emit(msg)
        else:
            self.html_ready[str].emit('file://' + html_file)


class HelpExplorer(QWidget, DockMixin):
    """A widget for showing the documentation. It behaves somewhat similar
    to spyders object inspector plugin and can show restructured text either
    as html (if sphinx is installed) or as plain text. It furthermore has a
    browser to show html content

    Warnings
    --------
    The :class:`HelpBrowser` class is known to crash under PyQt4 when new web
    page domains are loaded. Hence you should disable the browsing to different
    remote websites and even disable intersphinx"""

    #: The viewer classes used by the help explorer. :class:`HelpExplorer`
    #: instances replace this attribute with the corresponding HelpMixin
    #: instance
    viewers = OrderedDict([('HTML help', UrlHelp), ('Plain text', TextHelp)])

    def __init__(self, *args, **kwargs):
        super(HelpExplorer, self).__init__(*args, **kwargs)
        self.vbox = vbox = QVBoxLayout()
        self.combo = QComboBox(parent=self)
        vbox.addWidget(self.combo)
        self.viewers = OrderedDict(
            [(key, cls(parent=self)) for key, cls in six.iteritems(
                self.viewers)])
        for key, ini in six.iteritems(self.viewers):
            self.combo.addItem(key)
            ini.hide()
            vbox.addWidget(ini)
        self.viewer = next(six.itervalues(self.viewers))
        self.viewer.show()
        self.combo.currentIndexChanged[str].connect(self.set_viewer)
        self.setLayout(vbox)

    def set_viewer(self, name):
        """Sets the current documentation viewer

        Parameters
        ----------
        name: str or object
            A string must be one of the :attr:`viewers` attribute. An object
            can be one of the values in the :attr:`viewers` attribute"""
        if isinstance(name, six.string_types) and name not in self.viewers:
            raise ValueError("Don't have a viewer named %s" % (name, ))
        elif not isinstance(name, six.string_types):
            viewer = name
        else:
            viewer = self.viewers[name]
        self.viewer.hide()
        self.viewer = viewer
        self.viewer.show()

    @docstrings.dedent
    def show_help(self, obj, oname=''):
        """
        Show the documentaion of the given object

        We first try to use the current viewer based upon it's
        :attr:`HelpMixin.can_document_object` attribute. If this does not work,
        we check the other viewers

        Parameters
        ----------
        %(HelpMixin.show_help.parameters)s"""
        if self.viewer.can_document_object:
            return self.viewer.show_help(obj, oname=oname)
        for i, viewer in enumerate(six.itervalues(self.viewers)):
            if viewer.can_document_object:
                self.set_viewer(viewer)
                self.combo.blockSignals(True)
                self.combo.setCurrentIndex(i)
                self.combo.blockSignals(False)
                return viewer.show_help(obj, oname=oname)

    @docstrings.dedent
    def show_rst(self, text, oname=''):
        """
        Show restructured text

        We first try to use the current viewer based upon it's
        :attr:`HelpMixin.can_show_rst` attribute. If this does not work,
        we check the other viewers

        Parameters
        ----------
        %(HelpMixin.show_rst.parameters)s"""
        if self.viewer.can_show_rst:
            return self.viewer.show_rst(text, oname=oname)
        for viewer in six.itervalues(self.viewers):
            if viewer.can_show_rst:
                self.set_viewer(viewer)
                return viewer.show_rst(text, oname=oname)
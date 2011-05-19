# -*- Mode: Python -*-
# vi:si:et:sw=4:sts=4:ts=4

# Morituri - for those about to RIP

# Copyright (C) 2009 Thomas Vander Stichele

# This file is part of morituri.
# 
# morituri is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
# 
# morituri is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
# 
# You should have received a copy of the GNU General Public License
# along with morituri.  If not, see <http://www.gnu.org/licenses/>.

import os
import math

import gobject
gobject.threads_init()

from morituri.common import logcommand, task, common, accurip, log
from morituri.common import drive, program
from morituri.result import result
from morituri.image import image, cue, table
from morituri.program import cdrdao, cdparanoia


class Rip(logcommand.LogCommand):
    summary = "rip CD"

    description = """
Rips a CD.

Tracks are named according to the track template:
 - %t: track number
 - %a: track artist
 - %n: track title
 - %s: track sort name

Discs are named according to the disc template:
 - %A: album artist
 - %S: album sort name
 - %d: disc title
"""

    def addOptions(self):
        # FIXME: get from config
        default = 0
        self.parser.add_option('-o', '--offset',
            action="store", dest="offset",
            help="sample read offset (defaults to %d)" % default,
            default=default)
        self.parser.add_option('-O', '--output-directory',
            action="store", dest="output_directory",
            help="output directory (defaults to current directory)")
        # FIXME: have a cache of these pickles somewhere
        self.parser.add_option('-T', '--toc-pickle',
            action="store", dest="toc_pickle",
            help="pickle to use for reading and writing the TOC",
            default=default)
        # FIXME: get from config
        default = '%A - %d/%t. %a - %n'
        self.parser.add_option('', '--track-template',
            action="store", dest="track_template",
            help="template for track file naming (default %s)" % default,
            default=default)
        default = '%A - %d/%A - %d'
        self.parser.add_option('', '--disc-template',
            action="store", dest="disc_template",
            help="template for disc file naming (default %s)" % default,
            default=default)
        default = 'flac'

        # here to avoid import gst eating our options
        from morituri.common import encode

        self.parser.add_option('', '--profile',
            action="store", dest="profile",
            help="profile for encoding (default '%s', choices '%s')" % (
                default, "', '".join(encode.PROFILES.keys())),
            default=default)
        self.parser.add_option('-U', '--unknown',
            action="store_true", dest="unknown",
            help="whether to continue ripping if the CD is unknown (%default)",
            default=False)
        default = 'flac'


    def handleOptions(self, options):
        options.track_template = options.track_template.decode('utf-8')
        options.disc_template = options.disc_template.decode('utf-8')

    def do(self, args):
        prog = program.Program()
        runner = task.SyncRunner()

        def function(r, t):
            r.run(t)

        # if the device is mounted (data session), unmount it
        device = self.parentCommand.options.device
        print 'Checking device', device

        prog.loadDevice(device)
        prog.unmountDevice(device)
        
        # first, read the normal TOC, which is fast
        ptoc = common.Persister(self.options.toc_pickle or None)
        if not ptoc.object:
            t = cdrdao.ReadTOCTask(device=device)
            function(runner, t)
            version = t.tasks[1].parser.version
            from pkg_resources import parse_version as V
            # we've built a cdrdao 1.2.3rc2 modified package with the patch
            if V(version) < V('1.2.3rc2p1'):
                print '''
Warning: cdrdao older than 1.2.3 has a pre-gap length bug.
See  http://sourceforge.net/tracker/?func=detail&aid=604751&group_id=2171&atid=102171
'''
            ptoc.persist(t.table)
        ittoc = ptoc.object
        assert ittoc.hasTOC()

        # already show us some info based on this
        prog.getRipResult(ittoc.getCDDBDiscId())
        print "CDDB disc id", ittoc.getCDDBDiscId()
        mbdiscid = ittoc.getMusicBrainzDiscId()
        print "MusicBrainz disc id", mbdiscid

        prog.metadata = prog.getMusicBrainz(ittoc, mbdiscid)

        if not prog.metadata:
            # fall back to FreeDB for lookup
            cddbid = ittoc.getCDDBValues()
            cddbmd = prog.getCDDB(cddbid)
            if cddbmd:
                print 'FreeDB identifies disc as %s' % cddbmd

            if not self.options.unknown:
                prog.ejectDevice(device)
                return -1

        # now, read the complete index table, which is slower
        itable = prog.getTable(runner, ittoc.getCDDBDiscId(), device)

        assert itable.getCDDBDiscId() == ittoc.getCDDBDiscId(), \
            "full table's id %s differs from toc id %s" % (
                itable.getCDDBDiscId(), ittoc.getCDDBDiscId())
        assert itable.getMusicBrainzDiscId() == ittoc.getMusicBrainzDiscId(), \
            "full table's mb id %s differs from toc id mb %s" % (
            itable.getMusicBrainzDiscId(), ittoc.getMusicBrainzDiscId())
        assert itable.getAccurateRipURL() == ittoc.getAccurateRipURL(), \
            "full table's AR URL %s differs from toc AR URL %s" % (
            itable.getAccurateRipURL(), ittoc.getAccurateRipURL())

        prog.outdir = (self.options.output_directory or os.getcwd())
        prog.outdir = prog.outdir.decode('utf-8')
        # here to avoid import gst eating our options
        from morituri.common import encode
        profile = encode.PROFILES[self.options.profile]()

        # result

        prog.result.offset = int(self.options.offset)
        prog.result.artist = prog.metadata and prog.metadata.artist or 'Unknown Artist'
        prog.result.title = prog.metadata and prog.metadata.title or 'Unknown Title'
        # cdio is optional for now
        try:
            import cdio
            _, prog.result.vendor, prog.result.model, __ = cdio.Device(device).get_hwinfo()
        except ImportError:
            print 'WARNING: pycdio not installed, cannot identify drive'
            prog.result.vendor = 'Unknown'
            prog.result.model = 'Unknown'

        # FIXME: turn this into a method
        def ripIfNotRipped(number):
            # we can have a previous result
            trackResult = prog.result.getTrackResult(number)
            if not trackResult:
                trackResult = result.TrackResult()
                prog.result.tracks.append(trackResult)

            path = prog.getPath(prog.outdir, self.options.track_template, 
                mbdiscid, number) + '.' + profile.extension
            trackResult.number = number
            
            assert type(path) is unicode, "%r is not unicode" % path
            trackResult.filename = path
            if number > 0:
                trackResult.pregap = itable.tracks[number - 1].getPregap()

            # FIXME: optionally allow overriding reripping
            if os.path.exists(path):
                print 'Verifying track %d of %d: %s' % (
                    number, len(itable.tracks),
                    os.path.basename(path).encode('utf-8'))
                if not prog.verifyTrack(runner, trackResult):
                    print 'Verification failed, reripping...'
                    os.unlink(path)

            if not os.path.exists(path):
                print 'Ripping track %d of %d: %s' % (
                    number, len(itable.tracks),
                    os.path.basename(path).encode('utf-8'))
                prog.ripTrack(runner, trackResult, 
                    offset=int(self.options.offset),
                    device=self.parentCommand.options.device,
                    profile=profile,
                    taglist=prog.getTagList(number),
                    what='track %d of %d' % (number, len(itable.tracks)))

                if trackResult.testcrc == trackResult.copycrc:
                    print 'Checksums match for track %d' % (number)
                else:
                    print 'ERROR: checksums did not match for track %d' % (
                        number)
                print 'Peak level: %.2f %%' % (math.sqrt(trackResult.peak) * 100.0, )
                print 'Rip quality: %.2f %%' % (trackResult.quality * 100.0, )

            # overlay this rip onto the Table
            if number == 0:
                # HTOA goes on index 0 of track 1
                itable.setFile(1, 0, path, ittoc.getTrackStart(1),
                    number)
            else:
                itable.setFile(number, 1, path, ittoc.getTrackLength(number),
                    number)

            prog.saveRipResult()


        # check for hidden track one audio
        htoapath = None
        htoa = prog.getHTOA()
        if htoa:
            start, stop = htoa
            print 'Found Hidden Track One Audio from frame %d to %d' % (
                start, stop)
                
            # rip it
            ripIfNotRipped(0)
            htoapath = prog.result.tracks[0].filename

        for i, track in enumerate(itable.tracks):
            # FIXME: rip data tracks differently
            if not track.audio:
                print 'WARNING: skipping data track %d, not implemented' % (
                    i + 1, )
                # FIXME: make it work for now
                track.indexes[1].relative = 0
                continue

            ripIfNotRipped(i + 1)

        ### write disc files
        discName = prog.getPath(prog.outdir, self.options.disc_template, 
            mbdiscid, 0)
        dirname = os.path.dirname(discName)
        if not os.path.exists(dirname):
            os.makedirs(dirname)

        self.debug('writing cue file for %r', discName)
        prog.writeCue(discName)

        # write .m3u file
        m3uPath = u'%s.m3u' % discName
        handle = open(m3uPath, 'w')
        handle.write(u'#EXTM3U\n')
        if htoapath:
            u = u'#EXTINF:%d,%s\n' % (
                itable.getTrackStart(1) / common.FRAMES_PER_SECOND,
                    os.path.basename(htoapath[:-4]))
            handle.write(u.encode('utf-8'))
            u = '%s\n' % os.path.basename(htoapath)
            handle.write(u.encode('utf-8'))

        for i, track in enumerate(itable.tracks):
            if not track.audio:
                continue

            path = prog.getPath(prog.outdir, self.options.track_template, 
                mbdiscid, i + 1) + '.' + profile.extension
            u = u'#EXTINF:%d,%s\n' % (
                itable.getTrackLength(i + 1) / common.FRAMES_PER_SECOND,
                os.path.basename(path))
            handle.write(u.encode('utf-8'))
            u = '%s\n' % os.path.basename(path)
            handle.write(u.encode('utf-8'))
        handle.close()

        # verify using accuraterip
        url = ittoc.getAccurateRipURL()
        print "AccurateRip URL", url

        cache = accurip.AccuCache()
        responses = cache.retrieve(url)

        if not responses:
            print 'Album not found in AccurateRip database'

        if responses:
            print '%d AccurateRip reponses found' % len(responses)

            if responses[0].cddbDiscId != itable.getCDDBDiscId():
                print "AccurateRip response discid different: %s" % \
                    responses[0].cddbDiscId

           
        prog.verifyImage(runner, responses)

        print "\n".join(prog.getAccurateRipResults()) + "\n"

        # write log file
        logger = result.getLogger()
        prog.writeLog(discName, logger)

        prog.ejectDevice(device)


class CD(logcommand.LogCommand):
    summary = "handle CD's"

    subCommandClasses = [Rip, ]

    def addOptions(self):
        self.parser.add_option('-d', '--device',
            action="store", dest="device",
            help="CD-DA device")
 
    def handleOptions(self, options):
        if not options.device:
            drives = drive.getAllDevicePaths()
            if not drives:
                self.error('No CD-DA drives found!')
                return 3
        
            # pick the first
            self.options.device = drives[0]

        # this can be a symlink to another device
        self.options.device = os.path.realpath(self.options.device)
